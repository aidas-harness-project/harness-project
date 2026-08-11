"""Inventory a PDF corpus and extract embedded loss-adjustment reports.

This is a corpus-research utility, not a case-pipeline stage. It never mutates
source PDFs and never reads or writes the harness's governed data/ or outputs/
trees. OCR cache and derived artifacts live under an explicit study directory.

Typical workflow:
    python tools/extract_loss_adjustment_sections.py scan sources STUDY_DIR
    # review every range in STUDY_DIR/boundary-review.json
    python tools/extract_loss_adjustment_sections.py apply-review STUDY_DIR
    python tools/extract_loss_adjustment_sections.py extract STUDY_DIR
    python tools/extract_loss_adjustment_sections.py validate STUDY_DIR
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import fitz


REPORT_TITLE_PATTERNS = (
    re.compile(r"손해사정서"),
    re.compile(r"보험금사정서"),
    re.compile(r"損害(?:査|查)定"),
    re.compile(r"CLAIMADJUSTMENT", re.IGNORECASE),
)
REPORT_BODY_PATTERNS = (
    re.compile(r"손해사정(?:을)?완료"),
    re.compile(r"사정요약"),
    re.compile(r"사정근거"),
    re.compile(r"사정금액"),
    re.compile(r"사정내역"),
    re.compile(r"사정의견"),
    re.compile(r"사정결과(?:및)?의견"),
    re.compile(r"보험금사정"),
    re.compile(r"보상금사정"),
    re.compile(r"손해배상금사정"),
    re.compile(r"책임사정사"),
    re.compile(r"손해사정사"),
)
EVIDENCE_LIST_PATTERN = re.compile(r"(?:VI|VII|Ⅵ|Ⅶ)?증빙자료")
ATTACHMENT_START_PATTERNS = (
    re.compile(r"^진단서(?:질병분류기호|환자의성명|병명)"),
    re.compile(r"^후유장해진단서"),
    re.compile(r"^입[·.]?퇴원확인서"),
    re.compile(r"^의무기록"),
    re.compile(r"^보험증권"),
    re.compile(r"^수술기록지"),
    re.compile(r"^OPERATIONRECORD", re.IGNORECASE),
    re.compile(r"^PATIENTID", re.IGNORECASE),
)
DOCUMENT_ID_PATTERN = re.compile(r"^DOC_[0-9]{3}$")
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def compact_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"[^0-9A-Za-z가-힣一-龥]+", "", normalized)


def _require_document_id(value: Any) -> str:
    if not isinstance(value, str) or DOCUMENT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid document_id: {value!r}")
    return value


def _confined_path(
    root: Path,
    value: Any,
    *,
    field_name: str,
    expected_relative: str | None = None,
    require_exists: bool = False,
) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"unsafe {field_name}: expected a relative path")
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or (expected_relative is not None and relative.as_posix() != expected_relative)
    ):
        raise ValueError(f"unsafe {field_name}: {value!r}")
    root_resolved = root.resolve()
    candidate = root / relative
    if not candidate.resolve(strict=False).is_relative_to(root_resolved):
        raise ValueError(f"unsafe {field_name}: {value!r}")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"unsafe {field_name}: symlink component in {value!r}")
    if require_exists and not candidate.is_file():
        raise ValueError(f"missing {field_name}: {value!r}")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _validate_study_root(study_root: Path) -> Path:
    study_resolved = study_root.resolve(strict=False)
    allowed_repository_source = (REPOSITORY_ROOT / "sources").resolve(strict=False)
    forbidden_study_roots = [
        allowed_repository_source,
        *[
            (REPOSITORY_ROOT / relative).resolve(strict=False)
            for relative in ("data", "outputs", "source-cases", "archive/sources")
        ],
    ]
    if any(_is_within(study_resolved, root) for root in forbidden_study_roots):
        raise ValueError("study root points into immutable or governed harness state")
    return study_resolved


def _validate_roots(source_root: Path, study_root: Path) -> tuple[Path, Path]:
    source_resolved = source_root.resolve(strict=True)
    study_resolved = _validate_study_root(study_root)
    if not source_resolved.is_dir():
        raise ValueError("source root must be a directory")
    if _is_within(study_resolved, source_resolved) or _is_within(
        source_resolved, study_resolved
    ):
        raise ValueError("source and study directories must not contain one another")

    allowed_repository_source = (REPOSITORY_ROOT / "sources").resolve(strict=False)
    forbidden_source_roots = [
        (REPOSITORY_ROOT / relative).resolve(strict=False)
        for relative in ("data", "outputs", "source-cases", "archive/sources")
    ]
    if _is_within(source_resolved, REPOSITORY_ROOT) and not _is_within(
        source_resolved, allowed_repository_source
    ):
        raise ValueError("source root is not the repository's approved sources tree")
    if any(_is_within(source_resolved, root) for root in forbidden_source_roots):
        raise ValueError("source root points into governed harness state")

    return source_resolved, study_resolved


def _manifest_roots(manifest: dict[str, Any], study_root: Path) -> tuple[Path, Path]:
    source_value = manifest.get("source_root")
    study_value = manifest.get("study_root")
    if not isinstance(source_value, str) or not isinstance(study_value, str):
        raise ValueError("manifest must declare source_root and study_root")
    source_resolved, declared_study = _validate_roots(
        Path(source_value), Path(study_value)
    )
    if declared_study != study_root.resolve(strict=False):
        raise ValueError("study path does not match manifest study_root")
    return source_resolved, declared_study


def _load_study_manifest(
    study_root: Path,
) -> tuple[Path, dict[str, Any], Path, Path]:
    """Validate the study boundary before reading its canonical manifest."""
    resolved_study = _validate_study_root(study_root)
    manifest_path = _confined_path(
        resolved_study,
        "manifest.json",
        field_name="manifest path",
        expected_relative="manifest.json",
        require_exists=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_root, declared_study = _manifest_roots(manifest, resolved_study)
    return manifest_path, manifest, source_root, declared_study


def _resolve_manifest_source(
    source_root: Path, record: dict[str, Any]
) -> Path:
    expected = _confined_path(
        source_root,
        record.get("source_relative_path"),
        field_name="source_relative_path",
    )
    try:
        expected_resolved = expected.resolve(strict=True)
        declared_resolved = Path(record.get("source_path", "")).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("manifest source path is missing") from exc
    if declared_resolved != expected_resolved:
        raise ValueError("source path does not match manifest root")
    return expected_resolved


def _page_signals(text: str) -> dict[str, Any]:
    compact = compact_text(text)
    titles = [pattern.pattern for pattern in REPORT_TITLE_PATTERNS if pattern.search(compact)]
    body = [pattern.pattern for pattern in REPORT_BODY_PATTERNS if pattern.search(compact)]
    return {
        "title_matches": titles,
        "body_matches": body,
        "evidence_list": bool(EVIDENCE_LIST_PATTERN.search(compact)),
        "attachment_start": any(pattern.search(compact[:240]) for pattern in ATTACHMENT_START_PATTERNS),
        "score": len(titles) * 8 + len(body) * 2,
        "snippet": " ".join(text.split())[:240],
    }


def detect_report_range(pages: list[str]) -> dict[str, Any]:
    """Return a conservative candidate range with 1-based inclusive pages."""
    signals = [_page_signals(page) for page in pages]
    title_pages = [
        index for index, signal in enumerate(signals) if signal["title_matches"]
    ]
    strong_pages = [
        index
        for index, signal in enumerate(signals)
        if signal["title_matches"] or signal["score"] >= 6
    ]
    body_pages = [index for index, signal in enumerate(signals) if signal["body_matches"]]
    total_score = sum(signal["score"] for signal in signals)
    contains_report = bool(title_pages) or (
        bool(strong_pages)
        and (
            len(body_pages) >= 2
            or total_score >= 12
            or (len(pages) <= 3 and total_score >= 6)
        )
    )
    evidence = [
        {
            "page": index + 1,
            "score": signal["score"],
            "title_matches": signal["title_matches"],
            "body_matches": signal["body_matches"],
            "evidence_list": signal["evidence_list"],
            "snippet": signal["snippet"],
        }
        for index, signal in enumerate(signals)
        if signal["score"] or signal["evidence_list"]
    ]
    if not contains_report:
        return {
            "contains_report": False,
            "page_start": None,
            "page_end": None,
            "confidence": "high" if total_score == 0 else "low",
            "detection_evidence": evidence,
        }

    first_signal = min(strong_pages + body_pages)
    page_start = 1 if first_signal <= 2 else first_signal + 1

    evidence_pages = [
        index
        for index, signal in enumerate(signals)
        if index >= first_signal and signal["evidence_list"]
    ]
    if evidence_pages:
        page_end = evidence_pages[0] + 1
        confidence = "high"
    else:
        attachment_pages = [
            index
            for index, signal in enumerate(signals)
            if index > first_signal and signal["attachment_start"]
        ]
        last_body = max(body_pages or strong_pages)
        if attachment_pages and attachment_pages[0] > last_body:
            page_end = attachment_pages[0]
            confidence = "medium"
        else:
            page_end = last_body + 1
            confidence = "low"

    if page_end < page_start:
        page_end = page_start
        confidence = "low"
    return {
        "contains_report": True,
        "page_start": page_start,
        "page_end": page_end,
        "confidence": confidence,
        "detection_evidence": evidence,
    }


def split_sidecar_pages(sidecar_text: str, expected_page_count: int) -> list[str]:
    pages = sidecar_text.split("\f")
    if len(pages) == expected_page_count + 1 and not pages[-1].strip():
        pages.pop()
    if len(pages) != expected_page_count:
        raise ValueError(
            f"OCR sidecar has {len(pages)} pages; source PDF has {expected_page_count}"
        )
    return pages


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_pdf_range(
    source: Path, target: Path, page_start: int, page_end: int
) -> dict[str, Any]:
    if page_start < 1 or page_end < page_start:
        raise ValueError(f"invalid page range {page_start}-{page_end}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with fitz.open(source) as document:
        if page_end > document.page_count:
            raise ValueError(
                f"page range {page_start}-{page_end} exceeds {document.page_count} pages"
            )
        with fitz.open() as section:
            section.insert_pdf(
                document, from_page=page_start - 1, to_page=page_end - 1
            )
            section.save(temporary)
    os.replace(temporary, target)
    return {
        "source_sha256": sha256_file(source),
        "output_sha256": sha256_file(target),
        "page_start": page_start,
        "page_end": page_end,
        "output_page_count": page_end - page_start + 1,
    }


def write_section_text(
    target: Path, pages: list[str], page_start: int, page_end: int
) -> None:
    rendered = _format_section_text(pages, page_start, page_end)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, target)


def _format_section_text(
    pages: list[str], page_start: int, page_end: int
) -> str:
    if page_start < 1 or page_end > len(pages) or page_end < page_start:
        raise ValueError(f"invalid text page range {page_start}-{page_end}")
    blocks = []
    for page_number in range(page_start, page_end + 1):
        text = pages[page_number - 1].strip()
        blocks.append(f"===== SOURCE PAGE {page_number} =====\n{text}")
    return "\n\n".join(blocks) + "\n"


def _source_inventory(source_root: Path) -> list[dict[str, Any]]:
    paths = sorted(
        (
            path
            for path in source_root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        key=lambda path: unicodedata.normalize(
            "NFC", path.relative_to(source_root).as_posix()
        ),
    )
    records = []
    for index, path in enumerate(paths, start=1):
        relative = path.relative_to(source_root).as_posix()
        path = _confined_path(
            source_root,
            relative,
            field_name="source PDF path",
            expected_relative=relative,
            require_exists=True,
        )
        with fitz.open(path) as document:
            page_count = document.page_count
        records.append(
            {
                "document_id": f"DOC_{index:03d}",
                "source_path": path.as_posix(),
                "source_relative_path": relative,
                "source_sha256": sha256_file(path),
                "source_size_bytes": path.stat().st_size,
                "source_page_count": page_count,
            }
        )
    return records


def _embedded_pages(path: Path) -> list[str]:
    with fitz.open(path) as document:
        return [str(page.get_text("text")) for page in document]


def _ocr_document(
    record: dict[str, Any],
    cache_root: Path,
    working_root: Path,
    ocr_jobs: int,
) -> dict[str, Any]:
    document_id = _require_document_id(record.get("document_id"))
    source = Path(record["source_path"])
    source_sha256 = sha256_file(source)
    if source_sha256 != record.get("source_sha256"):
        raise ValueError(f"{document_id}: source hash changed before OCR")
    cache_path = cache_root / f"{document_id}.txt"
    cache_metadata_path = cache_root / f"{document_id}.json"
    if cache_path.exists() and cache_metadata_path.exists():
        try:
            sidecar_text = cache_path.read_text(encoding="utf-8")
            metadata = json.loads(cache_metadata_path.read_text(encoding="utf-8"))
            valid_metadata = (
                metadata.get("schema_version") == "ocr_cache.v1"
                and metadata.get("document_id") == document_id
                and metadata.get("source_sha256") == source_sha256
                and metadata.get("source_page_count") == record["source_page_count"]
                and metadata.get("sidecar_sha256") == sha256_text(sidecar_text)
                and metadata.get("ocr_method")
                in {"embedded_text", "ocrmypdf_tesseract_kor_eng"}
            )
            if valid_metadata:
                pages = split_sidecar_pages(
                    sidecar_text, record["source_page_count"]
                )
                return {
                    "pages": pages,
                    "ocr_method": metadata["ocr_method"],
                    "cache_reused": True,
                    "cache_path": cache_path,
                    "cache_metadata_path": cache_metadata_path,
                }
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    embedded = _embedded_pages(source)
    if embedded and all(len(page.strip()) >= 40 for page in embedded):
        sidecar_text = "\f".join(embedded)
        method = "embedded_text"
    else:
        working_root.mkdir(parents=True, exist_ok=True)
        temp_pdf = working_root / f"{record['document_id']}.pdf"
        temp_sidecar = working_root / f"{record['document_id']}.txt"
        command = [
            "ocrmypdf",
            "--force-ocr",
            "--output-type",
            "pdf",
            "--optimize",
            "0",
            "--jobs",
            str(ocr_jobs),
            "--language",
            "kor+eng",
            "--sidecar",
            str(temp_sidecar),
            str(source),
            str(temp_pdf),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        temp_pdf.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ocrmypdf failed for {source}: {result.stderr.strip()[-2000:]}"
            )
        sidecar_text = temp_sidecar.read_text(encoding="utf-8")
        temp_sidecar.unlink(missing_ok=True)
        method = "ocrmypdf_tesseract_kor_eng"

    pages = split_sidecar_pages(sidecar_text, record["source_page_count"])
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".txt.tmp")
    temporary.write_text(sidecar_text, encoding="utf-8")
    os.replace(temporary, cache_path)
    atomic_write_json(
        cache_metadata_path,
        {
            "schema_version": "ocr_cache.v1",
            "document_id": document_id,
            "source_sha256": source_sha256,
            "source_page_count": record["source_page_count"],
            "sidecar_sha256": sha256_text(sidecar_text),
            "ocr_method": method,
        },
    )
    return {
        "pages": pages,
        "ocr_method": method,
        "cache_reused": False,
        "cache_path": cache_path,
        "cache_metadata_path": cache_metadata_path,
    }


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _load_verified_ocr_cache(
    study_root: Path, record: dict[str, Any]
) -> tuple[Path, list[str], dict[str, Any]]:
    document_id = _require_document_id(record.get("document_id"))
    cache_relative = f"_ocr_cache/{document_id}.txt"
    metadata_relative = f"_ocr_cache/{document_id}.json"
    cache_path = _confined_path(
        study_root,
        record.get("ocr_cache_path"),
        field_name="ocr_cache_path",
        expected_relative=cache_relative,
    )
    metadata_path = _confined_path(
        study_root,
        record.get("ocr_cache_metadata_path"),
        field_name="ocr_cache_metadata_path",
        expected_relative=metadata_relative,
    )
    try:
        sidecar_text = cache_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        valid = (
            metadata.get("schema_version") == "ocr_cache.v1"
            and metadata.get("document_id") == document_id
            and metadata.get("source_sha256") == record.get("source_sha256")
            and metadata.get("source_page_count") == record.get("source_page_count")
            and metadata.get("sidecar_sha256") == sha256_text(sidecar_text)
            and metadata.get("ocr_method") == record.get("ocr_method")
        )
        if not valid:
            raise ValueError
        pages = split_sidecar_pages(sidecar_text, record["source_page_count"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{document_id}: OCR cache provenance mismatch") from exc
    return cache_path, pages, metadata


def apply_boundary_review(manifest_path: Path, review_path: Path) -> dict[str, Any]:
    """Validate and atomically apply one reviewed decision per source document."""
    study_root = _validate_study_root(manifest_path.parent)
    manifest_path = _confined_path(
        study_root,
        "manifest.json",
        field_name="manifest path",
        expected_relative="manifest.json",
        require_exists=True,
    )
    try:
        review_relative = review_path.resolve(strict=False).relative_to(study_root)
    except (OSError, ValueError) as exc:
        raise ValueError("boundary review file must be inside the study root") from exc
    review_path = _confined_path(
        study_root,
        review_relative.as_posix(),
        field_name="boundary review path",
        expected_relative="boundary-review.json",
        require_exists=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _manifest_roots(manifest, study_root)
    review_bytes = review_path.read_bytes()
    review = json.loads(review_bytes)
    records = manifest.get("documents", [])
    decisions = review.get("decisions", [])
    manifest_ids = [record.get("document_id") for record in records]
    decision_ids = [decision.get("document_id") for decision in decisions]
    for document_id in [*manifest_ids, *decision_ids]:
        _require_document_id(document_id)
    if (
        len(set(manifest_ids)) != len(manifest_ids)
        or len(set(decision_ids)) != len(decision_ids)
        or set(manifest_ids) != set(decision_ids)
        or review.get("document_count") != len(records)
    ):
        raise ValueError("boundary review must exactly cover the manifest documents")

    decisions_by_id = {decision["document_id"]: decision for decision in decisions}
    for record in records:
        decision = decisions_by_id[record["document_id"]]
        if any(
            decision.get(field) != record.get(field)
            for field in (
                "source_relative_path",
                "source_sha256",
                "source_page_count",
            )
        ):
            raise ValueError(
                f"{record['document_id']} review does not bind the current source generation"
            )
        if decision.get("primary_review") != "verified":
            raise ValueError(f"{record['document_id']} is not marked verified")
        rationale = decision.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"{record['document_id']} has no review rationale")
        contains_report = decision.get("contains_report")
        page_start = decision.get("page_start")
        page_end = decision.get("page_end")
        if contains_report is True:
            if (
                not isinstance(page_start, int)
                or not isinstance(page_end, int)
                or page_start < 1
                or page_end < page_start
                or page_end > record["source_page_count"]
            ):
                raise ValueError(f"{record['document_id']} has an invalid reviewed range")
        elif contains_report is False:
            if page_start is not None or page_end is not None:
                raise ValueError(
                    f"{record['document_id']} is no-report but has a page range"
                )
        else:
            raise ValueError(f"{record['document_id']} has no final classification")
        record.update(
            {
                "contains_report": contains_report,
                "page_start": page_start,
                "page_end": page_end,
                "review_status": "verified",
                "review_rationale": rationale,
            }
        )

    manifest["boundary_review"] = {
        "path": review_relative.as_posix(),
        "sha256": hashlib.sha256(review_bytes).hexdigest(),
        "document_count": len(decisions),
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def scan_corpus(
    source_root: Path,
    study_root: Path,
    document_jobs: int = 4,
    ocr_jobs: int = 3,
) -> dict[str, Any]:
    source_root, study_root = _validate_roots(source_root, study_root)
    inventory = _source_inventory(source_root)
    cache_root = study_root / "_ocr_cache"
    working_root = study_root / "_working"
    errors: list[dict[str, str]] = []

    def process(record: dict[str, Any]) -> dict[str, Any]:
        extracted = _ocr_document(record, cache_root, working_root, ocr_jobs)
        detection = detect_report_range(extracted["pages"])
        return {
            **record,
            "ocr_method": extracted["ocr_method"],
            "ocr_cache_reused": extracted["cache_reused"],
            "ocr_cache_path": Path(extracted["cache_path"])
            .relative_to(study_root)
            .as_posix(),
            "ocr_cache_metadata_path": Path(extracted["cache_metadata_path"])
            .relative_to(study_root)
            .as_posix(),
            **detection,
            "review_status": "pending",
            "outputs": None,
        }

    by_id: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=document_jobs) as executor:
        futures = {executor.submit(process, record): record for record in inventory}
        for future in concurrent.futures.as_completed(futures):
            record = futures[future]
            try:
                processed = future.result()
            except Exception as exc:  # keep full-corpus accounting on per-file failure
                errors.append(
                    {
                        "document_id": record["document_id"],
                        "source_path": record["source_path"],
                        "error": str(exc),
                    }
                )
                processed = {
                    **record,
                    "ocr_method": None,
                    "ocr_cache_reused": None,
                    "ocr_cache_path": None,
                    "ocr_cache_metadata_path": None,
                    "contains_report": None,
                    "page_start": None,
                    "page_end": None,
                    "confidence": "failed",
                    "detection_evidence": [],
                    "review_status": "blocked_ocr_failure",
                    "outputs": None,
                }
            by_id[record["document_id"]] = processed
            print(
                f"[{len(by_id)}/{len(inventory)}] {record['document_id']} "
                f"{processed['review_status']} {record['source_relative_path']}",
                flush=True,
            )

    shutil.rmtree(working_root, ignore_errors=True)
    manifest = {
        "schema_version": "loss_adjustment_corpus_manifest.v0.1",
        "source_root": source_root.as_posix(),
        "study_root": study_root.as_posix(),
        "document_count": len(inventory),
        "documents": [by_id[record["document_id"]] for record in inventory],
        "errors": errors,
    }
    atomic_write_json(study_root / "manifest.json", manifest)
    return manifest

def refresh_cache_provenance(
    study_root: Path,
    document_jobs: int = 4,
    ocr_jobs: int = 3,
) -> dict[str, Any]:
    manifest_path, manifest, source_root, study_root = _load_study_manifest(study_root)
    records = manifest.get("documents", [])
    cache_root = study_root / "_ocr_cache"
    working_root = study_root / "_working"

    def process(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        document_id = _require_document_id(record.get("document_id"))
        source = _resolve_manifest_source(source_root, record)
        if sha256_file(source) != record.get("source_sha256"):
            raise ValueError(f"{document_id}: source hash changed")
        extracted = _ocr_document(
            {**record, "source_path": source.as_posix()},
            cache_root,
            working_root,
            ocr_jobs,
        )
        return document_id, {
            "ocr_method": extracted["ocr_method"],
            "ocr_cache_reused": extracted["cache_reused"],
            "ocr_cache_path": Path(extracted["cache_path"])
            .relative_to(study_root)
            .as_posix(),
            "ocr_cache_metadata_path": Path(extracted["cache_metadata_path"])
            .relative_to(study_root)
            .as_posix(),
        }

    refreshed: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=document_jobs) as executor:
        futures = {executor.submit(process, record): record for record in records}
        for future in concurrent.futures.as_completed(futures):
            record = futures[future]
            try:
                document_id, fields = future.result()
                refreshed[document_id] = fields
            except Exception as exc:
                failures.append(f"{record.get('document_id')}: {exc}")
    shutil.rmtree(working_root, ignore_errors=True)
    if failures:
        raise RuntimeError("cache provenance refresh failed: " + "; ".join(failures))
    if len(refreshed) != len(records):
        raise RuntimeError("cache provenance refresh did not cover every manifest record")
    for record in records:
        record.update(refreshed[record["document_id"]])
    atomic_write_json(manifest_path, manifest)
    return manifest


def _remove_superseded_output_directory(study_root: Path, relative: str) -> None:
    path = _confined_path(
        study_root,
        relative,
        field_name="superseded output directory",
        expected_relative=relative,
    )
    if path.exists():
        shutil.rmtree(path)


def extract_sections(study_root: Path) -> dict[str, Any]:
    manifest_path, manifest, source_root, study_root = _load_study_manifest(study_root)
    records = manifest.get("documents", [])
    if not isinstance(records, list):
        raise ValueError("manifest documents must be an array")
    for record in records:
        _require_document_id(record.get("document_id"))
        source = _resolve_manifest_source(source_root, record)
        if sha256_file(source) != record.get("source_sha256"):
            raise ValueError(f"{record['document_id']}: source hash changed")
        if record.get("review_status") != "verified":
            raise ValueError(
                f"{record['document_id']} is not verified; review manifest page ranges first"
            )
    review_errors = _validate_review_artifacts(study_root, manifest, records)
    if review_errors:
        raise ValueError("review artifact validation failed: " + "; ".join(review_errors))
    extracted_count = 0
    no_report_metadata_count = 0
    for record in records:
        _require_document_id(record.get("document_id"))
        source = _resolve_manifest_source(source_root, record)
        if sha256_file(source) != record.get("source_sha256"):
            raise ValueError(f"{record['document_id']}: source hash changed")
        if record["review_status"] != "verified":
            raise ValueError(
                f"{record['document_id']} is not verified; review manifest page ranges first"
            )
        _, pages, _ = _load_verified_ocr_cache(study_root, record)
        if record["contains_report"] is False:
            metadata_relative = (
                Path("no-report") / record["document_id"] / "metadata.json"
            ).as_posix()
            metadata_path = _confined_path(
                study_root,
                metadata_relative,
                field_name="no-report output path",
                expected_relative=metadata_relative,
            )
            metadata = {
                "document_id": record["document_id"],
                "contains_report": False,
                "source_path": record["source_path"],
                "source_relative_path": record["source_relative_path"],
                "source_sha256": record["source_sha256"],
                "source_page_count": record["source_page_count"],
                "review_status": record["review_status"],
                "review_rationale": record.get("review_rationale"),
            }
            atomic_write_json(metadata_path, metadata)
            record["outputs"] = {
                "metadata": metadata_path.relative_to(study_root).as_posix()
            }
            _remove_superseded_output_directory(
                study_root, f"sections/{record['document_id']}"
            )
            no_report_metadata_count += 1
            continue
        if record["contains_report"] is not True:
            continue
        page_start = record["page_start"]
        page_end = record["page_end"]
        if not isinstance(page_start, int) or not isinstance(page_end, int):
            raise ValueError(f"{record['document_id']} has no valid page range")
        output_relative = (Path("sections") / record["document_id"]).as_posix()
        output_dir = _confined_path(
            study_root,
            output_relative,
            field_name="section output directory",
            expected_relative=output_relative,
        )
        pdf_path = _confined_path(
            study_root,
            f"{output_relative}/loss-adjustment-report.pdf",
            field_name="section PDF output path",
            expected_relative=f"{output_relative}/loss-adjustment-report.pdf",
        )
        text_path = _confined_path(
            study_root,
            f"{output_relative}/loss-adjustment-report.txt",
            field_name="section text output path",
            expected_relative=f"{output_relative}/loss-adjustment-report.txt",
        )
        metadata_path = _confined_path(
            study_root,
            f"{output_relative}/metadata.json",
            field_name="section metadata output path",
            expected_relative=f"{output_relative}/metadata.json",
        )
        metadata = {
            "document_id": record["document_id"],
            "source_path": record["source_path"],
            "source_relative_path": record["source_relative_path"],
            **extract_pdf_range(source, pdf_path, page_start, page_end),
            "text_sha256": None,
        }
        write_section_text(text_path, pages, page_start, page_end)
        metadata["text_sha256"] = sha256_file(text_path)
        atomic_write_json(metadata_path, metadata)
        record["outputs"] = {
            "directory": output_dir.relative_to(study_root).as_posix(),
            "pdf": pdf_path.relative_to(study_root).as_posix(),
            "text": text_path.relative_to(study_root).as_posix(),
            "metadata": metadata_path.relative_to(study_root).as_posix(),
        }
        _remove_superseded_output_directory(
            study_root, f"no-report/{record['document_id']}"
        )
        extracted_count += 1
    manifest["extracted_report_count"] = extracted_count
    manifest["no_report_metadata_count"] = no_report_metadata_count
    atomic_write_json(manifest_path, manifest)
    return manifest


def _page_render_sha256(page: fitz.Page) -> str:
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(1, 1), colorspace=fitz.csRGB, alpha=False, annots=True
    )
    digest = hashlib.sha256()
    digest.update(f"{pixmap.width}x{pixmap.height}:".encode("ascii"))
    digest.update(pixmap.samples)
    return digest.hexdigest()


def _validate_review_artifacts(
    study_root: Path, manifest: dict[str, Any], records: list[dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    reference = manifest.get("boundary_review")
    try:
        record_ids = [record.get("document_id") for record in records]
        for document_id in record_ids:
            _require_document_id(document_id)
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("manifest contains duplicate document IDs")
        if not isinstance(reference, dict):
            raise ValueError("boundary review reference missing")
        boundary_path = _confined_path(
            study_root,
            reference.get("path"),
            field_name="boundary review path",
            expected_relative="boundary-review.json",
        )
        if sha256_file(boundary_path) != reference.get("sha256"):
            raise ValueError("boundary review hash mismatch")
        boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
        if (
            boundary.get("document_count") != len(records)
            or reference.get("document_count") != len(records)
            or boundary.get("independent_review_status") != "verified"
        ):
            raise ValueError("boundary review coverage/status mismatch")
        decisions = boundary.get("decisions", [])
        if len(decisions) != len(records):
            raise ValueError("boundary review decisions do not close over manifest")
        by_id = {decision.get("document_id"): decision for decision in decisions}
        if len(by_id) != len(decisions):
            raise ValueError("boundary review contains duplicate document IDs")
        for record in records:
            decision = by_id.get(record.get("document_id"))
            if not isinstance(decision, dict):
                raise ValueError(
                    f"{record.get('document_id')}: boundary decision mismatch"
                )
            if any(
                decision.get(field) != record.get(field)
                for field in (
                    "source_relative_path",
                    "source_sha256",
                    "source_page_count",
                )
            ):
                raise ValueError(
                    f"{record.get('document_id')}: boundary decision source generation mismatch"
                )
            if any(
                decision.get(field) != record.get(field)
                for field in ("contains_report", "page_start", "page_end")
            ):
                raise ValueError(
                    f"{record.get('document_id')}: boundary decision mismatch"
                )
            if (
                decision.get("primary_review") != "verified"
                or not str(decision.get("rationale", "")).strip()
            ):
                raise ValueError(
                    f"{record.get('document_id')}: boundary decision is not verified"
                )

        independent_reference = boundary.get("independent_review")
        if not isinstance(independent_reference, dict):
            raise ValueError("independent boundary review reference missing")
        independent_path = _confined_path(
            study_root,
            independent_reference.get("path"),
            field_name="independent boundary review path",
            expected_relative="independent-boundary-review.json",
        )
        if sha256_file(independent_path) != independent_reference.get("sha256"):
            raise ValueError("independent boundary review hash mismatch")
        independent = json.loads(independent_path.read_text(encoding="utf-8"))
        independent_decisions = independent.get("decisions", [])
        independent_by_id = {
            decision.get("document_id"): decision
            for decision in independent_decisions
        }
        if (
            independent.get("status") != "verified"
            or independent.get("decision_count") != len(records)
            or independent.get("mismatch_count") != 0
            or independent_reference.get("document_count") != len(records)
            or independent_reference.get("mismatch_count") != 0
            or len(independent_decisions) != len(records)
            or len(independent_by_id) != len(records)
        ):
            raise ValueError("independent boundary review coverage/status mismatch")
        for record in records:
            decision = independent_by_id.get(record.get("document_id"))
            if not isinstance(decision, dict):
                raise ValueError(
                    f"{record.get('document_id')}: independent boundary decision mismatch"
                )
            if any(
                decision.get(field) != record.get(field)
                for field in (
                    "source_relative_path",
                    "source_sha256",
                    "source_page_count",
                )
            ):
                raise ValueError(
                    f"{record.get('document_id')}: independent boundary decision source generation mismatch"
                )
            if any(
                decision.get(field) != record.get(field)
                for field in ("contains_report", "page_start", "page_end")
            ):
                raise ValueError(
                    f"{record.get('document_id')}: independent boundary decision mismatch"
                )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    return errors


def _directory_entries(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir()}


def validate_study(study_root: Path) -> dict[str, Any]:
    errors: list[str] = []
    report_count = 0
    no_report_count = 0
    try:
        manifest_path, manifest, source_root, study_root = _load_study_manifest(
            study_root
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "document_count": 0,
            "report_count": 0,
            "no_report_count": 0,
            "errors": [str(exc)],
            "valid": False,
        }

    records = manifest.get("documents", [])
    if not isinstance(records, list):
        records = []
        errors.append("manifest documents must be an array")
    if manifest.get("document_count") != len(records):
        errors.append("manifest document_count mismatch")

    try:
        live_inventory = _source_inventory(source_root)
        inventory_fields = (
            "document_id",
            "source_relative_path",
            "source_sha256",
            "source_size_bytes",
            "source_page_count",
        )
        expected_inventory = [
            {field: record.get(field) for field in inventory_fields}
            for record in records
        ]
        actual_inventory = [
            {field: record.get(field) for field in inventory_fields}
            for record in live_inventory
        ]
        if expected_inventory != actual_inventory:
            errors.append("manifest does not exactly close over live source PDF inventory")
    except (OSError, ValueError, RuntimeError) as exc:
        errors.append(f"source inventory validation failed: {exc}")

    errors.extend(_validate_review_artifacts(study_root, manifest, records))
    expected_report_ids: set[str] = set()
    expected_no_report_ids: set[str] = set()
    expected_cache_names: set[str] = set()

    for record in records:
        try:
            document_id = _require_document_id(record.get("document_id"))
            expected_cache_names.update({f"{document_id}.txt", f"{document_id}.json"})
            source = _resolve_manifest_source(source_root, record)
            if sha256_file(source) != record.get("source_sha256"):
                raise ValueError("source hash changed")
            _, pages, _ = _load_verified_ocr_cache(study_root, record)
            if record.get("review_status") != "verified":
                raise ValueError("manifest record is not verified")

            if record.get("contains_report") is True:
                report_count += 1
                expected_report_ids.add(document_id)
                output_root = f"sections/{document_id}"
                expected_outputs = {
                    "directory": output_root,
                    "pdf": f"{output_root}/loss-adjustment-report.pdf",
                    "text": f"{output_root}/loss-adjustment-report.txt",
                    "metadata": f"{output_root}/metadata.json",
                }
                if record.get("outputs") != expected_outputs:
                    raise ValueError("noncanonical report output paths")
                pdf_path = _confined_path(
                    study_root,
                    expected_outputs["pdf"],
                    field_name="section PDF output path",
                    expected_relative=expected_outputs["pdf"],
                )
                text_path = _confined_path(
                    study_root,
                    expected_outputs["text"],
                    field_name="section text output path",
                    expected_relative=expected_outputs["text"],
                )
                metadata_path = _confined_path(
                    study_root,
                    expected_outputs["metadata"],
                    field_name="section metadata output path",
                    expected_relative=expected_outputs["metadata"],
                )
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                page_start = record.get("page_start")
                page_end = record.get("page_end")
                expected_metadata = {
                    "document_id": document_id,
                    "source_path": record.get("source_path"),
                    "source_relative_path": record.get("source_relative_path"),
                    "source_sha256": record.get("source_sha256"),
                    "page_start": page_start,
                    "page_end": page_end,
                    "output_page_count": page_end - page_start + 1,
                }
                if any(
                    metadata.get(field) != value
                    for field, value in expected_metadata.items()
                ):
                    raise ValueError("section metadata provenance mismatch")
                if sha256_file(pdf_path) != metadata.get("output_sha256"):
                    raise ValueError("PDF hash mismatch")
                if sha256_file(text_path) != metadata.get("text_sha256"):
                    raise ValueError("text hash mismatch")
                expected_text = _format_section_text(pages, page_start, page_end)
                if text_path.read_text(encoding="utf-8") != expected_text:
                    raise ValueError("section text does not match reviewed OCR slice")
                with fitz.open(source) as source_pdf, fitz.open(pdf_path) as section_pdf:
                    if section_pdf.page_count != page_end - page_start + 1:
                        raise ValueError("extracted page count mismatch")
                    for output_index, source_index in enumerate(
                        range(page_start - 1, page_end)
                    ):
                        if _page_render_sha256(source_pdf[source_index]) != _page_render_sha256(
                            section_pdf[output_index]
                        ):
                            raise ValueError(
                                f"source page content mismatch at source page {source_index + 1}"
                            )
                actual_names = {path.name for path in metadata_path.parent.iterdir()}
                if actual_names != {
                    "loss-adjustment-report.pdf",
                    "loss-adjustment-report.txt",
                    "metadata.json",
                }:
                    raise ValueError("unexpected report output files")
            elif record.get("contains_report") is False:
                no_report_count += 1
                expected_no_report_ids.add(document_id)
                expected_metadata_relative = f"no-report/{document_id}/metadata.json"
                if record.get("outputs") != {"metadata": expected_metadata_relative}:
                    raise ValueError("noncanonical no-report output path")
                metadata_path = _confined_path(
                    study_root,
                    expected_metadata_relative,
                    field_name="no-report output path",
                    expected_relative=expected_metadata_relative,
                )
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expected_metadata = {
                    "document_id": document_id,
                    "contains_report": False,
                    "source_path": record.get("source_path"),
                    "source_relative_path": record.get("source_relative_path"),
                    "source_sha256": record.get("source_sha256"),
                    "source_page_count": record.get("source_page_count"),
                    "review_status": "verified",
                    "review_rationale": record.get("review_rationale"),
                }
                if metadata != expected_metadata:
                    raise ValueError("no-report metadata mismatch")
                actual_names = {path.name for path in metadata_path.parent.iterdir()}
                if actual_names != {"metadata.json"}:
                    raise ValueError("unexpected no-report output files")
            else:
                raise ValueError("unresolved OCR/classification")
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            fitz.FileDataError,
        ) as exc:
            errors.append(f"{record.get('document_id')}: {exc}")

    if _directory_entries(study_root / "sections") != expected_report_ids:
        errors.append("sections directory contains unexpected entries or misses report IDs")
    if _directory_entries(study_root / "no-report") != expected_no_report_ids:
        errors.append("no-report directory contains unexpected entries or misses no-report IDs")
    if _directory_entries(study_root / "_ocr_cache") != expected_cache_names:
        errors.append("OCR cache does not close over manifest documents")
    if manifest.get("extracted_report_count") != report_count:
        errors.append("manifest extracted_report_count mismatch")
    if manifest.get("no_report_metadata_count") != no_report_count:
        errors.append("manifest no_report_metadata_count mismatch")
    return {
        "document_count": len(records),
        "report_count": report_count,
        "no_report_count": no_report_count,
        "errors": errors,
        "valid": not errors,
    }


def adopt_reviewed_cache_provenance(study_root: Path) -> dict[str, Any]:
    """Bind legacy sidecars only after the reviewed study passes preflight proof."""
    manifest_path, manifest, source_root, resolved_study_root = _load_study_manifest(
        study_root
    )
    records = manifest.get("documents", [])
    if not isinstance(records, list):
        raise ValueError("manifest documents must be a list")

    preflight_errors = _validate_review_artifacts(
        resolved_study_root, manifest, records
    )
    live_inventory = _source_inventory(source_root)
    inventory_fields = (
        "document_id",
        "source_relative_path",
        "source_sha256",
        "source_size_bytes",
        "source_page_count",
    )
    if [
        {field: record.get(field) for field in inventory_fields}
        for record in records
    ] != [
        {field: record.get(field) for field in inventory_fields}
        for record in live_inventory
    ]:
        preflight_errors.append(
            "manifest does not exactly close over live source PDF inventory"
        )

    prepared: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    for record in records:
        document_id = record.get("document_id", "<unknown>")
        try:
            document_id = _require_document_id(document_id)
            source_path = _resolve_manifest_source(source_root, record)
            if sha256_file(source_path) != record.get("source_sha256"):
                raise ValueError("source hash mismatch")
            cache_path = _confined_path(
                resolved_study_root,
                record.get("ocr_cache_path"),
                field_name="ocr_cache_path",
                expected_relative=f"_ocr_cache/{document_id}.txt",
                require_exists=True,
            )
            sidecar_text = cache_path.read_text(encoding="utf-8")
            pages = split_sidecar_pages(
                sidecar_text, int(record.get("source_page_count", -1))
            )
            sidecar_hash = sha256_text(sidecar_text)

            metadata_path = _confined_path(
                resolved_study_root,
                f"_ocr_cache/{document_id}.json",
                field_name="OCR cache metadata path",
                expected_relative=f"_ocr_cache/{document_id}.json",
            )
            existing_metadata: dict[str, Any] = {}
            if metadata_path.is_file():
                try:
                    existing_metadata = json.loads(
                        metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    existing_metadata = {}
            recomputed_from_source = all(
                (
                    existing_metadata.get("schema_version") == "ocr_cache.v1",
                    existing_metadata.get("document_id") == document_id,
                    existing_metadata.get("source_sha256")
                    == record.get("source_sha256"),
                    existing_metadata.get("source_page_count")
                    == record.get("source_page_count"),
                    existing_metadata.get("sidecar_sha256") == sidecar_hash,
                )
            )

            if record.get("contains_report") is True:
                expected_outputs = {
                    "directory": f"sections/{document_id}",
                    "pdf": f"sections/{document_id}/loss-adjustment-report.pdf",
                    "text": f"sections/{document_id}/loss-adjustment-report.txt",
                    "metadata": f"sections/{document_id}/metadata.json",
                }
                if record.get("outputs") != expected_outputs:
                    raise ValueError("noncanonical report output paths")
                pdf_path = _confined_path(
                    resolved_study_root,
                    expected_outputs["pdf"],
                    field_name="report PDF path",
                    expected_relative=expected_outputs["pdf"],
                    require_exists=True,
                )
                text_path = _confined_path(
                    resolved_study_root,
                    expected_outputs["text"],
                    field_name="report text path",
                    expected_relative=expected_outputs["text"],
                    require_exists=True,
                )
                output_metadata_path = _confined_path(
                    resolved_study_root,
                    expected_outputs["metadata"],
                    field_name="report metadata path",
                    expected_relative=expected_outputs["metadata"],
                    require_exists=True,
                )
                output_metadata = json.loads(
                    output_metadata_path.read_text(encoding="utf-8")
                )
                if sha256_file(pdf_path) != output_metadata.get("output_sha256"):
                    raise ValueError("PDF hash mismatch")
                if sha256_file(text_path) != output_metadata.get("text_sha256"):
                    raise ValueError("text hash mismatch")
                page_start = int(record["page_start"])
                page_end = int(record["page_end"])
                with fitz.open(source_path) as source_pdf, fitz.open(pdf_path) as output_pdf:
                    if output_pdf.page_count != page_end - page_start + 1:
                        raise ValueError("PDF page count mismatch")
                    for offset, source_page_number in enumerate(
                        range(page_start - 1, page_end)
                    ):
                        if _page_render_sha256(source_pdf[source_page_number]) != _page_render_sha256(
                            output_pdf[offset]
                        ):
                            raise ValueError("selected source page content mismatch")
                if not recomputed_from_source:
                    expected_text = _format_section_text(pages, page_start, page_end)
                    if text_path.read_text(encoding="utf-8") != expected_text:
                        raise ValueError(
                            "legacy sidecar does not match reviewed extracted text"
                        )
            else:
                expected_outputs = {
                    "metadata": f"no-report/{document_id}/metadata.json"
                }
                if record.get("outputs") != expected_outputs:
                    raise ValueError("noncanonical no-report output path")
                output_metadata_path = _confined_path(
                    resolved_study_root,
                    expected_outputs["metadata"],
                    field_name="no-report metadata path",
                    expected_relative=expected_outputs["metadata"],
                    require_exists=True,
                )
                output_metadata = json.loads(
                    output_metadata_path.read_text(encoding="utf-8")
                )
                if output_metadata.get("source_sha256") != record.get("source_sha256"):
                    raise ValueError("no-report source hash mismatch")

            binding_basis = (
                "recomputed_from_source"
                if recomputed_from_source
                else "reviewed_legacy_sidecar"
            )
            ocr_method = (
                existing_metadata.get("ocr_method")
                if recomputed_from_source
                else "reviewed_legacy_sidecar"
            )
            cache_metadata = {
                "schema_version": "ocr_cache.v1",
                "document_id": document_id,
                "source_sha256": record["source_sha256"],
                "source_page_count": record["source_page_count"],
                "sidecar_sha256": sidecar_hash,
                "ocr_method": ocr_method,
                "binding_basis": binding_basis,
            }
            prepared.append((record, metadata_path, cache_metadata))
        except (KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
            preflight_errors.append(f"{document_id}: {exc}")

    if preflight_errors:
        raise ValueError(
            "reviewed cache provenance adoption failed: "
            + "; ".join(preflight_errors[:20])
        )

    for record, metadata_path, cache_metadata in prepared:
        atomic_write_json(metadata_path, cache_metadata)
        record["ocr_method"] = cache_metadata["ocr_method"]
        record["ocr_cache_metadata_path"] = (
            f"_ocr_cache/{record['document_id']}.json"
        )
        record.pop("ocr_cache_reused", None)
    manifest["cache_provenance"] = {
        "schema_version": "ocr_cache_provenance.v1",
        "document_count": len(prepared),
        "adoption_preconditions": [
            "verified primary and independent boundary artifacts",
            "exact live source inventory and source hashes",
            "sidecar page counts",
            "canonical derived output paths and hashes",
            "rendered equality of selected source and output pages",
            "legacy selected text equality or fresh source-recomputation metadata",
        ],
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan")
    scan.add_argument("source_root", type=Path)
    scan.add_argument("study_root", type=Path)
    scan.add_argument("--document-jobs", type=int, default=4)
    scan.add_argument("--ocr-jobs", type=int, default=3)
    refresh_cache = subparsers.add_parser("refresh-cache")
    refresh_cache.add_argument("study_root", type=Path)
    refresh_cache.add_argument("--document-jobs", type=int, default=4)
    refresh_cache.add_argument("--ocr-jobs", type=int, default=3)
    adopt_cache = subparsers.add_parser("adopt-reviewed-cache")
    adopt_cache.add_argument("study_root", type=Path)
    extract = subparsers.add_parser("extract")
    extract.add_argument("study_root", type=Path)
    apply_review = subparsers.add_parser("apply-review")
    apply_review.add_argument("study_root", type=Path)
    apply_review.add_argument("--review-file", type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("study_root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "scan":
        result = scan_corpus(
            args.source_root,
            args.study_root,
            document_jobs=args.document_jobs,
            ocr_jobs=args.ocr_jobs,
        )
        print(json.dumps({"documents": result["document_count"], "errors": result["errors"]}, ensure_ascii=False))
        return 1 if result["errors"] else 0
    if args.command == "refresh-cache":
        result = refresh_cache_provenance(
            args.study_root,
            document_jobs=args.document_jobs,
            ocr_jobs=args.ocr_jobs,
        )
        print(json.dumps({"refreshed": len(result["documents"])}, ensure_ascii=False))
        return 0
    if args.command == "adopt-reviewed-cache":
        result = adopt_reviewed_cache_provenance(args.study_root)
        print(
            json.dumps(
                {
                    "adopted": len(result["documents"]),
                    "cache_provenance": result["cache_provenance"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "extract":
        result = extract_sections(args.study_root)
        print(json.dumps({"extracted": result["extracted_report_count"]}, ensure_ascii=False))
        return 0
    if args.command == "apply-review":
        review_file = args.review_file or args.study_root / "boundary-review.json"
        result = apply_boundary_review(args.study_root / "manifest.json", review_file)
        print(
            json.dumps(
                {
                    "verified": sum(
                        record["review_status"] == "verified"
                        for record in result["documents"]
                    )
                },
                ensure_ascii=False,
            )
        )
        return 0
    result = validate_study(args.study_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
