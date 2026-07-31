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
import contextlib
import ctypes
import fcntl
import functools
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
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


def _command_version(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "unavailable:timeout"
    except OSError as exc:
        return f"unavailable:{exc.__class__.__name__}"
    first_line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    if completed.returncode != 0:
        return f"unavailable:exit-{completed.returncode}:{first_line}"
    return first_line or "unavailable:empty-version"


OCR_EXTRACTION_CONTRACT = {
    "schema_version": "ocr_extraction_contract.v2",
    "extractor_implementation_sha256": hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest(),
    "tool_versions": {
        "ocrmypdf": _command_version(["ocrmypdf", "--version"]),
        "pymupdf": fitz.VersionBind,
        "tesseract": _command_version(["tesseract", "--version"]),
    },
    "embedded_text_minimum_characters_per_page": 40,
    "ocr_engine": "ocrmypdf",
    "ocr_languages": "kor+eng",
    "ocr_force": True,
    "ocr_output_type": "pdf",
    "ocr_optimize": 0,
    "sidecar_page_separator": "form_feed",
}
OCR_EXTRACTION_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(OCR_EXTRACTION_CONTRACT, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
).hexdigest()
OCR_PROCESS_TIMEOUT_SECONDS = 1800.0
OCR_TERMINATE_GRACE_SECONDS = 5.0
_ACTIVE_STUDY_LOCK_DESCRIPTORS: set[int] = set()
_ACTIVE_STUDY_ROOT_DESCRIPTORS: set[int] = set()
_ACTIVE_STUDY_LOCKS_BY_ROOT: dict[Path, int] = {}
_ACTIVE_STUDY_LOCK_GUARD = threading.Lock()


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


def _reject_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            details = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(details.st_mode):
            raise ValueError(f"{label} must not contain symlink components")


def _validate_study_root(study_root: Path) -> Path:
    if (
        len(study_root.parts) == 5
        and study_root.parts[:4] == ("/", "proc", "self", "fd")
        and study_root.parts[4].isdigit()
    ):
        descriptor = int(study_root.parts[4])
        with _ACTIVE_STUDY_LOCK_GUARD:
            if descriptor in _ACTIVE_STUDY_ROOT_DESCRIPTORS:
                return study_root
    _reject_symlink_components(study_root, "study root")
    study_resolved = study_root.resolve(strict=False)
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    canonical_repository_study = (
        REPOSITORY_ROOT / "loss-adjustment-format-study"
    ).resolve(strict=False)
    if _is_within(study_resolved, repository_root) and (
        study_resolved != canonical_repository_study
    ):
        raise ValueError(
            "in-repository study root must be the canonical ignored location: loss-adjustment-format-study"
        )
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


def _is_descriptor_anchored_study_root(study_root: Path) -> bool:
    parts = study_root.parts
    return len(parts) == 5 and parts[:4] == ("/", "proc", "self", "fd")


def _validate_roots(source_root: Path, study_root: Path) -> tuple[Path, Path]:
    if source_root.is_symlink():
        raise ValueError("source root must not be a symlink")
    _reject_symlink_components(source_root, "source root")
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
    manifest = json.loads(_read_regular_file_no_follow(manifest_path))
    source_root, declared_study = _manifest_roots(manifest, resolved_study)
    return manifest_path, manifest, source_root, resolved_study


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


def _read_regular_file_no_follow(path: Path) -> bytes:
    file_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(file_fd)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"source is not a regular file: {path}")
        with os.fdopen(file_fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(file_fd)


def extract_pdf_range(
    source: Path | bytes, target: Path, page_start: int, page_end: int
) -> dict[str, Any]:
    if page_start < 1 or page_end < page_start:
        raise ValueError(f"invalid page range {page_start}-{page_end}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    source_bytes = (
        source if isinstance(source, bytes) else _read_regular_file_no_follow(source)
    )
    with fitz.open(stream=source_bytes, filetype="pdf") as document:
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
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
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


def _source_inventory_snapshot(
    source_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    if source_root.is_symlink():
        raise ValueError("source root must not be a symlink")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | no_follow
    root_fd = os.open(source_root, directory_flags)
    captured: list[tuple[str, bytes]] = []

    def walk(directory_fd: int, parent: tuple[str, ...]) -> None:
        for name in os.listdir(directory_fd):
            details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative_parts = (*parent, name)
            relative = Path(*relative_parts).as_posix()
            if stat.S_ISLNK(details.st_mode):
                raise ValueError(
                    f"source inventory must not contain symlinks: {relative}"
                )
            if stat.S_ISDIR(details.st_mode):
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    walk(child_fd, relative_parts)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(details.st_mode) and Path(name).suffix.lower() == ".pdf":
                file_fd = os.open(name, os.O_RDONLY | no_follow, dir_fd=directory_fd)
                try:
                    opened = os.fstat(file_fd)
                    if not stat.S_ISREG(opened.st_mode):
                        raise ValueError(f"source PDF is not a regular file: {relative}")
                    with os.fdopen(file_fd, "rb", closefd=False) as handle:
                        captured.append((relative, handle.read()))
                finally:
                    os.close(file_fd)

    try:
        walk(root_fd, ())
    finally:
        os.close(root_fd)

    captured.sort(
        key=lambda item: (unicodedata.normalize("NFC", item[0]), item[0])
    )
    records = []
    source_bytes: dict[str, bytes] = {}
    for index, (relative, payload) in enumerate(captured, start=1):
        with fitz.open(stream=payload, filetype="pdf") as document:
            page_count = document.page_count
        source_bytes[relative] = payload
        records.append(
            {
                "document_id": f"DOC_{index:03d}",
                "source_path": (source_root / relative).as_posix(),
                "source_relative_path": relative,
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "source_size_bytes": len(payload),
                "source_page_count": page_count,
            }
        )
    return records, source_bytes


def _source_inventory(source_root: Path) -> list[dict[str, Any]]:
    records, _ = _source_inventory_snapshot(source_root)
    return records


SOURCE_INVENTORY_FIELDS = (
    "document_id",
    "source_relative_path",
    "source_sha256",
    "source_size_bytes",
    "source_page_count",
)


def _require_live_inventory_closure(
    source_root: Path, records: list[dict[str, Any]]
) -> dict[str, bytes]:
    expected = [
        {field: record.get(field) for field in SOURCE_INVENTORY_FIELDS}
        for record in records
    ]
    actual_inventory, source_bytes = _source_inventory_snapshot(source_root)
    actual = [
        {field: record.get(field) for field in SOURCE_INVENTORY_FIELDS}
        for record in actual_inventory
    ]
    if expected != actual:
        raise ValueError("manifest does not exactly close over live source PDF inventory")
    return source_bytes


def _embedded_pages(source_bytes: bytes) -> list[str]:
    with fitz.open(stream=source_bytes, filetype="pdf") as document:
        return [str(page.get_text("text")) for page in document]


def _active_study_lock_fds() -> tuple[int, ...]:
    with _ACTIVE_STUDY_LOCK_GUARD:
        return tuple(sorted(_ACTIVE_STUDY_LOCK_DESCRIPTORS))


def _study_lock_fd_for_path(path: Path) -> int | None:
    resolved = path.resolve(strict=False)
    with _ACTIVE_STUDY_LOCK_GUARD:
        matches = {
            descriptor
            for root, descriptor in _ACTIVE_STUDY_LOCKS_BY_ROOT.items()
            if path == root
            or path.is_relative_to(root)
            or resolved == root.resolve(strict=False)
            or resolved.is_relative_to(root.resolve(strict=False))
        }
    if len(matches) > 1:
        raise RuntimeError("path is owned by multiple active study locks")
    return next(iter(matches)) if matches else None


def _terminate_ocr_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=OCR_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def _run_ocr_command(
    command: list[str], study_lock_descriptor: int | None = None
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        pass_fds=(study_lock_descriptor,) if study_lock_descriptor is not None else (),
    )
    try:
        stdout, stderr = process.communicate(timeout=OCR_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_ocr_process_group(process)
        raise RuntimeError(
            f"ocrmypdf timed out after {OCR_PROCESS_TIMEOUT_SECONDS:g} seconds"
        ) from exc
    except BaseException:
        _terminate_ocr_process_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _ocr_document(
    record: dict[str, Any],
    cache_root: Path,
    working_root: Path,
    ocr_jobs: int,
    source_bytes: bytes | None = None,
) -> dict[str, Any]:
    document_id = _require_document_id(record.get("document_id"))
    source = Path(record["source_path"])
    source_bytes = (
        _read_regular_file_no_follow(source) if source_bytes is None else source_bytes
    )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
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
                and metadata.get("extraction_contract_sha256")
                == OCR_EXTRACTION_CONTRACT_SHA256
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
                    "ocr_sidecar_sha256": metadata["sidecar_sha256"],
                    "cache_reused": True,
                    "cache_path": cache_path,
                    "cache_metadata_path": cache_metadata_path,
                }
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    embedded = _embedded_pages(source_bytes)
    if embedded and all(len(page.strip()) >= 40 for page in embedded):
        sidecar_text = "\f".join(embedded)
        method = "embedded_text"
    else:
        working_root.mkdir(parents=True, exist_ok=True)
        temp_source = working_root / f"{record['document_id']}.source.pdf"
        temp_pdf = working_root / f"{record['document_id']}.pdf"
        temp_sidecar = working_root / f"{record['document_id']}.txt"
        temp_source.write_bytes(source_bytes)
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
            str(temp_source),
            str(temp_pdf),
        ]
        try:
            result = _run_ocr_command(command, _study_lock_fd_for_path(cache_root))
            if result.returncode != 0:
                raise RuntimeError(
                    f"ocrmypdf failed for {source}: {result.stderr.strip()[-2000:]}"
                )
            sidecar_text = temp_sidecar.read_text(encoding="utf-8")
        finally:
            temp_source.unlink(missing_ok=True)
            temp_pdf.unlink(missing_ok=True)
            temp_sidecar.unlink(missing_ok=True)
        method = "ocrmypdf_tesseract_kor_eng"

    pages = split_sidecar_pages(sidecar_text, record["source_page_count"])
    sidecar_sha256 = sha256_text(sidecar_text)
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".txt.tmp")
    temporary.write_text(sidecar_text, encoding="utf-8")
    os.replace(temporary, cache_path)
    atomic_write_json(
        cache_metadata_path,
        {
            "schema_version": "ocr_cache.v1",
            "extraction_contract_sha256": OCR_EXTRACTION_CONTRACT_SHA256,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "source_page_count": record["source_page_count"],
            "sidecar_sha256": sidecar_sha256,
            "ocr_method": method,
        },
    )
    return {
        "pages": pages,
        "ocr_method": method,
        "ocr_sidecar_sha256": sidecar_sha256,
        "cache_reused": False,
        "cache_path": cache_path,
        "cache_metadata_path": cache_metadata_path,
    }


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _durable_create_json(path: Path, value: Any) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".create", dir=path.parent
    )
    temporary = Path(temporary_name)
    temporary_object = _publication_object_token(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _renameat2(temporary, path, _RENAME_NOREPLACE)
        _fsync_directory(path.parent)
        installed_object = _publication_object_token(path)
        if installed_object != temporary_object:
            raise RuntimeError("durable JSON create changed identity")
        return installed_object
    finally:
        if temporary.exists():
            _remove_publication_path_verified(temporary, temporary_object)
            _fsync_directory(temporary.parent)


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


def _renameat2(source: Path, target: Path, flags: int) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("renameat2 is required for object-bound publication")
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{source} -> {target}")


def _fsync_rename_parents(left: Path, right: Path) -> None:
    for parent in {left.parent, right.parent}:
        _fsync_directory(parent)


def _publication_object_token(path: Path) -> dict[str, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("publication generation must not be a symlink")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "type": stat.S_IFMT(metadata.st_mode),
    }


def _valid_publication_object_token(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"device", "inode", "type"}
        and isinstance(value["device"], int)
        and not isinstance(value["device"], bool)
        and value["device"] >= 0
        and isinstance(value["inode"], int)
        and not isinstance(value["inode"], bool)
        and value["inode"] > 0
        and value["type"] in {stat.S_IFREG, stat.S_IFDIR}
    )


def _publication_generation_matches(
    path: Path, expected_sha256: str, expected_object: dict[str, int]
) -> bool:
    return (
        path.exists()
        and _publication_object_token(path) == expected_object
        and _publication_fingerprint(path) == expected_sha256
    )


def _capture_publication_generation(path: Path) -> tuple[str, dict[str, int]]:
    before = _publication_object_token(path)
    fingerprint = _publication_fingerprint(path)
    after = _publication_object_token(path)
    if after != before:
        raise RuntimeError("publication generation changed while fingerprinting")
    return fingerprint, before


def _exchange_verified(
    left: Path,
    left_sha256: str,
    left_object: dict[str, int],
    right: Path,
    right_sha256: str,
    right_object: dict[str, int],
) -> None:
    if not _publication_generation_matches(
        left, left_sha256, left_object
    ) or not _publication_generation_matches(right, right_sha256, right_object):
        raise RuntimeError("publication exchange preimage changed")
    _renameat2(left, right, _RENAME_EXCHANGE)
    _fsync_rename_parents(left, right)
    if _publication_generation_matches(
        left, right_sha256, right_object
    ) and _publication_generation_matches(right, left_sha256, left_object):
        return
    raise RuntimeError("publication exchange identity changed")


def _move_noreplace_verified(
    source: Path,
    source_sha256: str,
    source_object: dict[str, int],
    target: Path,
) -> None:
    if not _publication_generation_matches(
        source, source_sha256, source_object
    ) or target.exists():
        raise RuntimeError("publication move preimage changed")
    _renameat2(source, target, _RENAME_NOREPLACE)
    _fsync_rename_parents(source, target)
    if not source.exists() and _publication_generation_matches(
        target, source_sha256, source_object
    ):
        return
    raise RuntimeError("publication move identity changed")


def _remove_directory_contents_fd(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IFMT(metadata.st_mode),
            ):
                raise RuntimeError("publication cleanup member changed")
            os.unlink(name, dir_fd=descriptor)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("publication cleanup contains unsupported member")
        child = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError("publication cleanup directory changed")
            _remove_directory_contents_fd(child)
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError("publication cleanup directory changed")
            os.rmdir(name, dir_fd=descriptor)
        finally:
            os.close(child)


def _remove_publication_path_verified(
    path: Path, expected_object: dict[str, int]
) -> None:
    if _publication_object_token(path) != expected_object:
        raise RuntimeError("publication cleanup object changed")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if expected_object["type"] == stat.S_IFDIR:
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        opened_object = {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "type": stat.S_IFMT(metadata.st_mode),
        }
        if opened_object != expected_object:
            raise RuntimeError("publication cleanup object changed")
        if stat.S_ISDIR(metadata.st_mode):
            _remove_directory_contents_fd(descriptor)
        if _publication_object_token(path) != expected_object:
            raise RuntimeError("publication cleanup name changed")
        if stat.S_ISDIR(metadata.st_mode):
            path.rmdir()
        else:
            path.unlink()
    finally:
        os.close(descriptor)


def _quarantine_and_remove_verified(
    path: Path, expected_sha256: str, expected_object: dict[str, int]
) -> None:
    quarantine = path.with_name(f".remove-{path.name}")
    _move_noreplace_verified(path, expected_sha256, expected_object, quarantine)
    if not _publication_generation_matches(
        quarantine, expected_sha256, expected_object
    ):
        raise RuntimeError("publication cleanup quarantine changed")
    _remove_publication_path_verified(quarantine, expected_object)
    _fsync_directory(quarantine.parent)


def _pending_cleanup_path(
    path: Path, expected_sha256: str, expected_object: dict[str, int]
) -> Path | None:
    quarantine = path.with_name(f".remove-{path.name}")
    existing = [candidate for candidate in (path, quarantine) if candidate.exists()]
    if len(existing) > 1:
        raise RuntimeError("publication cleanup has duplicate generations")
    if not existing:
        return None
    actual = existing[0]
    if not _publication_generation_matches(
        actual, expected_sha256, expected_object
    ):
        raise RuntimeError("publication cleanup generation changed")
    return actual


def _publication_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(current: Path, relative: bytes) -> None:
        metadata = current.lstat()
        add(relative)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"publication artifact contains a symlink: {current}")
        if stat.S_ISREG(metadata.st_mode):
            add(b"file")
            add(_read_regular_file_no_follow(current))
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"publication artifact contains an unsupported entry: {current}")
        add(b"directory")
        for child in sorted(current.iterdir(), key=lambda item: os.fsencode(item.name)):
            child_relative = relative + b"/" + os.fsencode(child.name)
            visit(child, child_relative)

    visit(path, b".")
    return digest.hexdigest()


def _publication_paths(
    study_root: Path, target: dict[str, Any], staging_relative: str
) -> tuple[Path, Path, Path]:
    relative = target.get("relative")
    candidate_relative = target.get("candidate_relative")
    backup_relative = target.get("backup_relative")
    if relative not in {"_ocr_cache", "sections", "no-report", "manifest.json"}:
        raise ValueError("publication journal contains an unsafe target")
    if not isinstance(candidate_relative, str) or not isinstance(backup_relative, str):
        raise ValueError("publication journal contains invalid paths")
    expected_candidate = f"{staging_relative}/{relative}"
    expected_backup = f"{staging_relative}/backup-{Path(relative).name}"
    if candidate_relative != expected_candidate or backup_relative != expected_backup:
        raise ValueError("publication journal path does not match its target")
    final_path = _confined_path(
        study_root,
        relative,
        field_name="publication target",
        expected_relative=relative,
    )
    candidate_path = _confined_path(
        study_root,
        candidate_relative,
        field_name="publication candidate",
        expected_relative=candidate_relative,
    )
    backup_path = _confined_path(
        study_root,
        backup_relative,
        field_name="publication backup",
        expected_relative=backup_relative,
    )
    return final_path, candidate_path, backup_path


def _staging_names(staging_root: Path) -> set[str]:
    if not staging_root.exists():
        return set()
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise RuntimeError("publication staging root changed identity")
    return {entry.name for entry in staging_root.iterdir()}


def _write_publication_phase(
    journal_path: Path, journal: dict[str, Any], phase: str
) -> dict[str, int]:
    journal["phase"] = phase
    _durable_write_json(journal_path, journal)
    return _publication_object_token(journal_path)


def _finish_publication_phase(
    study_root: Path,
    journal_path: Path,
    journal_object: dict[str, int],
    journal: dict[str, Any],
    staging_root: Path,
    staging_object: dict[str, int],
    resolved: list[tuple[dict[str, Any], Path, Path, Path]],
) -> None:
    phase = journal["phase"]
    cleanup: list[tuple[Path, str, dict[str, int]]] = []
    allowed_names: set[str] = set()
    for target, final_path, candidate_path, backup_path in resolved:
        if phase == "committed":
            if not _publication_generation_matches(
                final_path,
                target["candidate_sha256"],
                target["candidate_object"],
            ):
                raise RuntimeError("committed publication target changed")
            if candidate_path.exists():
                raise RuntimeError("committed publication retained a candidate")
            if target["had_previous"]:
                pending = _pending_cleanup_path(
                    backup_path,
                    target["previous_sha256"],
                    target["previous_object"],
                )
                if pending is not None:
                    allowed_names.add(pending.name)
                    cleanup.append(
                        (
                            pending,
                            target["previous_sha256"],
                            target["previous_object"],
                        )
                    )
        else:
            if target["had_previous"]:
                if not _publication_generation_matches(
                    final_path,
                    target["previous_sha256"],
                    target["previous_object"],
                ):
                    raise RuntimeError("rolled-back publication target changed")
            elif final_path.exists():
                raise RuntimeError("rolled-back publication target unexpectedly exists")
            if backup_path.exists():
                raise RuntimeError("rolled-back publication retained a backup")
            pending = _pending_cleanup_path(
                candidate_path,
                target["candidate_sha256"],
                target["candidate_object"],
            )
            if pending is not None:
                allowed_names.add(pending.name)
                cleanup.append(
                    (
                        pending,
                        target["candidate_sha256"],
                        target["candidate_object"],
                    )
                )
    if _staging_names(staging_root) != allowed_names:
        raise RuntimeError("publication staging inventory is not transaction-closed")
    for path, fingerprint, expected_object in cleanup:
        if path.name.startswith(".remove-"):
            if not _publication_generation_matches(
                path, fingerprint, expected_object
            ):
                raise RuntimeError("publication cleanup quarantine changed")
            _remove_publication_path_verified(path, expected_object)
            _fsync_directory(path.parent)
        else:
            _quarantine_and_remove_verified(path, fingerprint, expected_object)
    if staging_root.exists():
        _remove_publication_path_verified(staging_root, staging_object)
        _fsync_directory(staging_root.parent)
    _remove_publication_path_verified(journal_path, journal_object)
    _fsync_directory(study_root)
    try:
        (study_root / "_working").rmdir()
    except OSError:
        pass


def _recover_publication(study_root: Path) -> None:
    journal_path = _confined_path(
        study_root,
        ".publication-journal.json",
        field_name="publication journal",
        expected_relative=".publication-journal.json",
    )
    if not journal_path.exists():
        return
    journal_object = _publication_object_token(journal_path)
    journal = json.loads(_read_regular_file_no_follow(journal_path))
    if _publication_object_token(journal_path) != journal_object:
        raise RuntimeError("publication journal changed while reading")
    if journal.get("schema_version") != "loss_adjustment_publication_journal.v1":
        raise ValueError("unsupported publication journal schema")
    phase = journal.get("phase", "prepared")
    if phase not in {"prepared", "committed", "rolled_back"}:
        raise ValueError("publication journal has an invalid phase")
    journal["phase"] = phase
    staging_relative = journal.get("staging_relative")
    if not isinstance(staging_relative, str) or not re.fullmatch(
        r"_working/(?:scan|refresh-cache|extract|adopt-cache)-[A-Za-z0-9._-]+",
        staging_relative,
    ):
        raise ValueError("publication journal has an unsafe staging directory")
    staging_root = _confined_path(
        study_root,
        staging_relative,
        field_name="publication staging directory",
        expected_relative=staging_relative,
    )
    staging_object = journal.get("staging_object")
    if not _valid_publication_object_token(staging_object) or not stat.S_ISDIR(
        staging_object["type"]
    ):
        raise ValueError("publication journal lacks its staging object")
    if staging_root.exists() and _publication_object_token(staging_root) != staging_object:
        raise RuntimeError("publication staging object changed")
    if phase == "prepared" and not staging_root.exists():
        raise RuntimeError("prepared publication staging object is missing")
    operation = staging_relative.split("/", 1)[1].split("-", 1)[0]
    expected_targets = (
        {"_ocr_cache", "manifest.json"}
        if operation in {"scan", "refresh", "adopt"}
        else {"sections", "no-report", "manifest.json"}
    )
    targets = journal.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("publication journal has no targets")
    seen: set[str] = set()
    resolved: list[tuple[dict[str, Any], Path, Path, Path]] = []
    for target in targets:
        if not isinstance(target, dict) or not isinstance(
            target.get("had_previous"), bool
        ):
            raise ValueError("publication journal target is invalid")
        previous_sha256 = target.get("previous_sha256")
        candidate_sha256 = target.get("candidate_sha256")
        previous_object = target.get("previous_object")
        candidate_object = target.get("candidate_object")
        if not isinstance(candidate_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", candidate_sha256
        ) is None:
            raise ValueError("publication journal target lacks its candidate fingerprint")
        if not _valid_publication_object_token(candidate_object):
            raise ValueError("publication journal target lacks its candidate object")
        if target["had_previous"] and (
            not isinstance(previous_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", previous_sha256) is None
        ):
            raise ValueError("publication journal target lacks its prior fingerprint")
        if target["had_previous"] and not _valid_publication_object_token(
            previous_object
        ):
            raise ValueError("publication journal target lacks its prior object")
        if not target["had_previous"] and previous_sha256 is not None:
            raise ValueError("new publication target has a prior fingerprint")
        if not target["had_previous"] and previous_object is not None:
            raise ValueError("new publication target has a prior object")
        relative = target.get("relative")
        if not isinstance(relative, str) or relative in seen:
            raise ValueError("publication journal target is duplicated")
        seen.add(relative)
        resolved.append(
            (target, *_publication_paths(study_root, target, staging_relative))
        )
    if seen != expected_targets:
        raise ValueError("publication journal target set does not match its operation")
    if phase != "prepared":
        _finish_publication_phase(
            study_root,
            journal_path,
            journal_object,
            journal,
            staging_root,
            staging_object,
            resolved,
        )
        return

    states: list[str] = []
    expected_staging: set[str] = set()
    for target, final_path, candidate_path, backup_path in resolved:
        old = target.get("previous_sha256")
        new = target["candidate_sha256"]
        old_object = target.get("previous_object")
        new_object = target["candidate_object"]
        final_is_old = target["had_previous"] and _publication_generation_matches(
            final_path, old, old_object
        )
        final_is_new = _publication_generation_matches(
            final_path, new, new_object
        )
        candidate_is_old = target[
            "had_previous"
        ] and _publication_generation_matches(candidate_path, old, old_object)
        candidate_is_new = _publication_generation_matches(
            candidate_path, new, new_object
        )
        backup_is_old = target["had_previous"] and _publication_generation_matches(
            backup_path, old, old_object
        )
        backup_is_new = _publication_generation_matches(
            backup_path, new, new_object
        )
        final_missing = not final_path.exists()
        candidate_missing = not candidate_path.exists()
        backup_missing = not backup_path.exists()
        if (
            target["had_previous"]
            and final_is_old
            and candidate_is_new
            and backup_missing
        ):
            state = "untouched"
            expected_staging.add(candidate_path.name)
        elif (
            target["had_previous"]
            and final_is_new
            and candidate_is_old
            and backup_missing
        ):
            state = "exchanged"
            expected_staging.add(candidate_path.name)
        elif (
            target["had_previous"]
            and final_is_new
            and candidate_missing
            and backup_is_old
        ):
            state = "published"
            expected_staging.add(backup_path.name)
        elif (
            target["had_previous"]
            and final_is_old
            and candidate_missing
            and backup_is_new
        ):
            state = "restored"
            expected_staging.add(backup_path.name)
        elif (
            not target["had_previous"]
            and final_missing
            and candidate_is_new
            and backup_missing
        ):
            state = "untouched"
            expected_staging.add(candidate_path.name)
        elif (
            not target["had_previous"]
            and final_is_new
            and candidate_missing
            and backup_missing
        ):
            state = "published"
        else:
            raise RuntimeError(
                f"cannot recover publication target {target['relative']}: generation mismatch"
            )
        states.append(state)
    if _staging_names(staging_root) != expected_staging:
        raise RuntimeError("publication staging inventory is not transaction-closed")

    for state, (target, final_path, candidate_path, backup_path) in reversed(
        list(zip(states, resolved, strict=True))
    ):
        if state == "untouched":
            continue
        if state == "restored":
            _move_noreplace_verified(
                backup_path,
                target["candidate_sha256"],
                target["candidate_object"],
                candidate_path,
            )
            continue
        if target["had_previous"] and state == "exchanged":
            _exchange_verified(
                final_path,
                target["candidate_sha256"],
                target["candidate_object"],
                candidate_path,
                target["previous_sha256"],
                target["previous_object"],
            )
        elif target["had_previous"]:
            _exchange_verified(
                final_path,
                target["candidate_sha256"],
                target["candidate_object"],
                backup_path,
                target["previous_sha256"],
                target["previous_object"],
            )
            _move_noreplace_verified(
                backup_path,
                target["candidate_sha256"],
                target["candidate_object"],
                candidate_path,
            )
        else:
            _move_noreplace_verified(
                final_path,
                target["candidate_sha256"],
                target["candidate_object"],
                candidate_path,
            )
    journal_object = _write_publication_phase(
        journal_path, journal, "rolled_back"
    )
    _finish_publication_phase(
        study_root,
        journal_path,
        journal_object,
        journal,
        staging_root,
        staging_object,
        resolved,
    )


def _fsync_tree(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"publication candidate contains a symlink: {path}")
    if stat.S_ISREG(metadata.st_mode):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"publication candidate contains an unsupported entry: {path}")
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        _fsync_tree(child)
    _fsync_directory(path)


def _publish_generation(
    study_root: Path,
    staging_root: Path,
    relatives: list[str],
    pre_commit_check: Any | None = None,
) -> None:
    staging_relative = staging_root.relative_to(study_root).as_posix()
    if not re.fullmatch(
        r"_working/(?:scan|refresh-cache|extract|adopt-cache)-[A-Za-z0-9._-]+",
        staging_relative,
    ):
        raise ValueError("unsafe publication staging directory")
    if len(set(relatives)) != len(relatives) or not relatives:
        raise ValueError("publication targets must be unique and non-empty")
    operation = staging_relative.split("/", 1)[1].split("-", 1)[0]
    expected_targets = (
        {"_ocr_cache", "manifest.json"}
        if operation in {"scan", "refresh", "adopt"}
        else {"sections", "no-report", "manifest.json"}
    )
    if set(relatives) != expected_targets:
        raise ValueError("publication target set does not match its operation")
    targets: list[dict[str, Any]] = []
    for relative in relatives:
        if relative not in {"_ocr_cache", "sections", "no-report", "manifest.json"}:
            raise ValueError(f"unsafe publication target: {relative}")
        candidate_relative = f"{staging_relative}/{relative}"
        backup_relative = f"{staging_relative}/backup-{Path(relative).name}"
        final_path = study_root / relative
        had_previous = final_path.exists()
        previous_sha256 = None
        previous_object = None
        if had_previous:
            previous_sha256, previous_object = _capture_publication_generation(
                final_path
            )
        target = {
            "relative": relative,
            "candidate_relative": candidate_relative,
            "backup_relative": backup_relative,
            "had_previous": had_previous,
            "previous_sha256": previous_sha256,
            "previous_object": previous_object,
        }
        _, candidate_path, backup_path = _publication_paths(
            study_root, target, staging_relative
        )
        if not candidate_path.exists():
            raise ValueError(f"missing publication candidate: {relative}")
        if backup_path.exists():
            raise ValueError(f"publication backup already exists: {relative}")
        (
            target["candidate_sha256"],
            target["candidate_object"],
        ) = _capture_publication_generation(candidate_path)
        targets.append(target)
    if _staging_names(staging_root) != set(relatives):
        raise RuntimeError("publication staging inventory is not transaction-closed")
    _fsync_tree(staging_root)
    if pre_commit_check is not None:
        pre_commit_check()
    journal_path = _confined_path(
        study_root,
        ".publication-journal.json",
        field_name="publication journal",
        expected_relative=".publication-journal.json",
    )
    if journal_path.exists():
        raise RuntimeError("an unrecovered publication journal already exists")
    journal = {
        "schema_version": "loss_adjustment_publication_journal.v1",
        "phase": "prepared",
        "staging_relative": staging_relative,
        "staging_object": _publication_object_token(staging_root),
        "targets": targets,
    }
    journal_object = _durable_create_json(journal_path, journal)
    try:
        for target in targets:
            final_path, candidate_path, backup_path = _publication_paths(
                study_root, target, staging_relative
            )
            if target["had_previous"]:
                _exchange_verified(
                    final_path,
                    target["previous_sha256"],
                    target["previous_object"],
                    candidate_path,
                    target["candidate_sha256"],
                    target["candidate_object"],
                )
                _move_noreplace_verified(
                    candidate_path,
                    target["previous_sha256"],
                    target["previous_object"],
                    backup_path,
                )
            else:
                _move_noreplace_verified(
                    candidate_path,
                    target["candidate_sha256"],
                    target["candidate_object"],
                    final_path,
                )
        resolved = [
            (target, *_publication_paths(study_root, target, staging_relative))
            for target in targets
        ]
        if pre_commit_check is not None:
            pre_commit_check()
        journal_object = _write_publication_phase(
            journal_path, journal, "committed"
        )
        _finish_publication_phase(
            study_root,
            journal_path,
            journal_object,
            journal,
            staging_root,
            journal["staging_object"],
            resolved,
        )
    except BaseException:
        try:
            if not journal_path.exists():
                _durable_create_json(journal_path, journal)
            _recover_publication(study_root)
        except BaseException as recovery_error:
            raise RuntimeError(
                "publication failed and durable rollback could not complete; journal preserved"
            ) from recovery_error
        raise


def _cleanup_orphan_staging(study_root: Path) -> None:
    working_root = _confined_path(
        study_root,
        "_working",
        field_name="working directory",
        expected_relative="_working",
    )
    if not working_root.exists():
        return
    entries = list(working_root.iterdir())
    if entries:
        raise RuntimeError(
            "orphan staging exists without a publication journal; preserved for review"
        )
    try:
        working_root.rmdir()
    except OSError:
        pass


@contextlib.contextmanager
def _exclusive_study_lock(study_root: Path) -> Any:
    study_root.mkdir(parents=True, exist_ok=True)
    lock_root = study_root.resolve(strict=True)
    expected_root = study_root.stat(follow_symlinks=False)
    root_descriptor = os.open(
        study_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    anchored_root = Path(f"/proc/self/fd/{root_descriptor}")
    opened_root = os.fstat(root_descriptor)
    if (
        (opened_root.st_dev, opened_root.st_ino)
        != (expected_root.st_dev, expected_root.st_ino)
        or anchored_root.resolve(strict=True) != lock_root
    ):
        os.close(root_descriptor)
        raise RuntimeError("study root changed before lock acquisition")
    try:
        descriptor = os.open(
            ".study.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
    except BaseException:
        os.close(root_descriptor)
        raise
    registered = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another study mutation is active") from exc
        with _ACTIVE_STUDY_LOCK_GUARD:
            _ACTIVE_STUDY_LOCK_DESCRIPTORS.add(descriptor)
            _ACTIVE_STUDY_ROOT_DESCRIPTORS.add(root_descriptor)
            _ACTIVE_STUDY_LOCKS_BY_ROOT[lock_root] = descriptor
            _ACTIVE_STUDY_LOCKS_BY_ROOT[anchored_root] = descriptor
        registered = True
        _recover_publication(anchored_root)
        _cleanup_orphan_staging(anchored_root)
        yield anchored_root
    finally:
        if registered:
            with _ACTIVE_STUDY_LOCK_GUARD:
                _ACTIVE_STUDY_LOCK_DESCRIPTORS.discard(descriptor)
                _ACTIVE_STUDY_ROOT_DESCRIPTORS.discard(root_descriptor)
                if _ACTIVE_STUDY_LOCKS_BY_ROOT.get(lock_root) == descriptor:
                    del _ACTIVE_STUDY_LOCKS_BY_ROOT[lock_root]
                if _ACTIVE_STUDY_LOCKS_BY_ROOT.get(anchored_root) == descriptor:
                    del _ACTIVE_STUDY_LOCKS_BY_ROOT[anchored_root]
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            os.close(root_descriptor)


def _locked_study_mutation(study_argument_index: int) -> Any:
    def decorate(function: Any) -> Any:
        @functools.wraps(function)
        def locked(*args: Any, **kwargs: Any) -> Any:
            positional = list(args)
            keyword = dict(kwargs)
            if len(positional) > study_argument_index:
                study_root = _validate_study_root(Path(positional[study_argument_index]))
                positional[study_argument_index] = study_root
            else:
                study_root = _validate_study_root(Path(keyword["study_root"]))
                keyword["study_root"] = study_root
            with _exclusive_study_lock(study_root) as anchored_root:
                if len(positional) > study_argument_index:
                    positional[study_argument_index] = anchored_root
                else:
                    keyword["study_root"] = anchored_root
                return function(*positional, **keyword)

        return locked

    return decorate


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
        sidecar_bytes = _read_regular_file_no_follow(cache_path)
        metadata_bytes = _read_regular_file_no_follow(metadata_path)
        sidecar_text = sidecar_bytes.decode("utf-8")
        metadata = json.loads(metadata_bytes)
        valid = (
            metadata.get("schema_version") == "ocr_cache.v1"
            and metadata.get("extraction_contract_sha256")
            == OCR_EXTRACTION_CONTRACT_SHA256
            and metadata.get("document_id") == document_id
            and metadata.get("source_sha256") == record.get("source_sha256")
            and metadata.get("source_page_count") == record.get("source_page_count")
            and metadata.get("sidecar_sha256")
            == hashlib.sha256(sidecar_bytes).hexdigest()
            and metadata.get("sidecar_sha256")
            == record.get("ocr_sidecar_sha256")
            and metadata.get("ocr_method") == record.get("ocr_method")
        )
        if not valid:
            raise ValueError
        pages = split_sidecar_pages(sidecar_text, record["source_page_count"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{document_id}: OCR cache provenance mismatch") from exc
    return cache_path, pages, metadata


def _apply_boundary_review_unlocked(
    manifest_path: Path, review_path: Path
) -> dict[str, Any]:
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
        review_relative = (
            review_path.relative_to(study_root)
            if _is_descriptor_anchored_study_root(study_root)
            else review_path.resolve(strict=False).relative_to(study_root)
        )
    except (OSError, ValueError) as exc:
        raise ValueError("boundary review file must be inside the study root") from exc
    review_path = _confined_path(
        study_root,
        review_relative.as_posix(),
        field_name="boundary review path",
        expected_relative="boundary-review.json",
        require_exists=True,
    )
    manifest = json.loads(_read_regular_file_no_follow(manifest_path))
    _manifest_roots(manifest, study_root)
    review = json.loads(_read_regular_file_no_follow(review_path))
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
                "ocr_sidecar_sha256",
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
        "sha256": sha256_file(review_path),
        "document_count": len(decisions),
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def apply_boundary_review(manifest_path: Path, review_path: Path) -> dict[str, Any]:
    study_root = _validate_study_root(manifest_path.parent)
    manifest_relative = manifest_path.resolve(strict=False).relative_to(study_root)
    try:
        review_relative = review_path.resolve(strict=False).relative_to(study_root)
    except (OSError, ValueError) as exc:
        raise ValueError("boundary review file must be inside the study root") from exc
    with _exclusive_study_lock(study_root) as anchored_root:
        return _apply_boundary_review_unlocked(
            anchored_root / manifest_relative, anchored_root / review_relative
        )


@_locked_study_mutation(1)
def scan_corpus(
    source_root: Path,
    study_root: Path,
    document_jobs: int = 4,
    ocr_jobs: int = 3,
) -> dict[str, Any]:
    source_root, study_root = _validate_roots(source_root, study_root)
    inventory, source_bytes_by_relative = _source_inventory_snapshot(source_root)
    final_cache_root = _confined_path(
        study_root,
        "_ocr_cache",
        field_name="OCR cache root",
        expected_relative="_ocr_cache",
    )
    working_root = _confined_path(
        study_root,
        "_working",
        field_name="working directory",
        expected_relative="_working",
    )
    if working_root.exists() and _directory_entries(working_root):
        raise ValueError("working directory contains stale runtime residue")
    working_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="scan-", dir=working_root))
    staged_cache_root = staging_root / "_ocr_cache"
    staged_worker_root = staging_root / "ocr-working"

    def cleanup_staging() -> None:
        shutil.rmtree(staging_root, ignore_errors=True)
        try:
            working_root.rmdir()
        except OSError:
            pass

    errors: list[dict[str, str]] = []

    def process(record: dict[str, Any]) -> dict[str, Any]:
        extracted = _ocr_document(
            record,
            staged_cache_root,
            staged_worker_root,
            ocr_jobs,
            source_bytes_by_relative[record["source_relative_path"]],
        )
        detection = detect_report_range(extracted["pages"])
        return {
            **record,
            "ocr_method": extracted["ocr_method"],
            "ocr_sidecar_sha256": extracted["ocr_sidecar_sha256"],
            "ocr_cache_reused": extracted["cache_reused"],
            "ocr_cache_path": f"_ocr_cache/{record['document_id']}.txt",
            "ocr_cache_metadata_path": f"_ocr_cache/{record['document_id']}.json",
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
                    "ocr_sidecar_sha256": None,
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

    shutil.rmtree(staged_worker_root, ignore_errors=True)
    if errors:
        cleanup_staging()
        raise RuntimeError(
            f"scan failed for {len(errors)} document(s); prior generation preserved"
        )
    manifest = {
        "schema_version": "loss_adjustment_corpus_manifest.v0.1",
        "source_root": source_root.as_posix(),
        "study_root": study_root.resolve(strict=True).as_posix(),
        "document_count": len(inventory),
        "documents": [by_id[record["document_id"]] for record in inventory],
        "errors": errors,
    }
    try:
        _require_live_inventory_closure(source_root, inventory)
    except Exception:
        cleanup_staging()
        raise
    staged_cache_root.mkdir(exist_ok=True)
    atomic_write_json(staging_root / "manifest.json", manifest)
    try:
        _publish_generation(
            study_root,
            staging_root,
            ["_ocr_cache", "manifest.json"],
            pre_commit_check=lambda: _require_live_inventory_closure(
                source_root, inventory
            ),
        )
    finally:
        if not (study_root / ".publication-journal.json").exists():
            cleanup_staging()
    return manifest


@_locked_study_mutation(0)
def refresh_cache_provenance(
    study_root: Path,
    document_jobs: int = 4,
    ocr_jobs: int = 3,
) -> dict[str, Any]:
    manifest_path, manifest, source_root, study_root = _load_study_manifest(study_root)
    records = manifest.get("documents", [])
    if not isinstance(records, list):
        raise ValueError("manifest documents must be an array")
    source_bytes_by_relative = _require_live_inventory_closure(source_root, records)
    final_cache_root = _confined_path(
        study_root,
        "_ocr_cache",
        field_name="OCR cache root",
        expected_relative="_ocr_cache",
    )
    working_root = _confined_path(
        study_root,
        "_working",
        field_name="working directory",
        expected_relative="_working",
    )
    if working_root.exists() and _directory_entries(working_root):
        raise ValueError("working directory contains stale runtime residue")
    working_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="refresh-cache-", dir=working_root))
    staged_cache_root = staging_root / "_ocr_cache"
    staged_worker_root = staging_root / "ocr-working"

    def cleanup_staging() -> None:
        shutil.rmtree(staging_root, ignore_errors=True)
        try:
            working_root.rmdir()
        except OSError:
            pass

    def process(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        document_id = _require_document_id(record.get("document_id"))
        source = _resolve_manifest_source(source_root, record)
        extracted = _ocr_document(
            {**record, "source_path": source.as_posix()},
            staged_cache_root,
            staged_worker_root,
            ocr_jobs,
            source_bytes_by_relative[record["source_relative_path"]],
        )
        return document_id, {
            "ocr_method": extracted["ocr_method"],
            "ocr_sidecar_sha256": extracted["ocr_sidecar_sha256"],
            "ocr_cache_reused": extracted["cache_reused"],
            "ocr_cache_path": f"_ocr_cache/{document_id}.txt",
            "ocr_cache_metadata_path": f"_ocr_cache/{document_id}.json",
            "detected_range": detect_report_range(extracted["pages"]),
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
    if failures:
        cleanup_staging()
        raise RuntimeError("cache provenance refresh failed: " + "; ".join(failures))
    if len(refreshed) != len(records):
        cleanup_staging()
        raise RuntimeError("cache provenance refresh did not cover every manifest record")
    expected_cache_names = {
        name
        for record in records
        for name in (
            f"{record['document_id']}.txt",
            f"{record['document_id']}.json",
        )
    }
    if _directory_entries(staged_cache_root) != expected_cache_names:
        cleanup_staging()
        raise RuntimeError("cache provenance refresh produced an incomplete projection")
    shutil.rmtree(staged_worker_root, ignore_errors=True)
    review_generation_changed = any(
        record.get("ocr_sidecar_sha256")
        != refreshed[record["document_id"]]["ocr_sidecar_sha256"]
        for record in records
    )
    for record in records:
        fields = dict(refreshed[record["document_id"]])
        detected_range = fields.pop("detected_range")
        record.update(fields)
        if review_generation_changed:
            record.update(detected_range)
            record["review_status"] = "pending"
            record["outputs"] = None
            record.pop("review_rationale", None)
    if review_generation_changed:
        manifest.pop("boundary_review", None)
        manifest.pop("extracted_report_count", None)
        manifest.pop("no_report_metadata_count", None)
    try:
        _require_live_inventory_closure(source_root, records)
    except Exception:
        cleanup_staging()
        raise

    atomic_write_json(staging_root / "manifest.json", manifest)
    try:
        _publish_generation(
            study_root,
            staging_root,
            ["_ocr_cache", "manifest.json"],
            pre_commit_check=lambda: _require_live_inventory_closure(
                source_root, records
            ),
        )
    finally:
        if not (study_root / ".publication-journal.json").exists():
            cleanup_staging()
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


@_locked_study_mutation(0)
def extract_sections(study_root: Path) -> dict[str, Any]:
    manifest_path, manifest, source_root, study_root = _load_study_manifest(study_root)
    records = manifest.get("documents", [])
    if not isinstance(records, list):
        raise ValueError("manifest documents must be an array")
    for record in records:
        _require_document_id(record.get("document_id"))
        _resolve_manifest_source(source_root, record)
        if record.get("review_status") != "verified":
            raise ValueError(
                f"{record['document_id']} is not verified; review manifest page ranges first"
            )
    review_errors = _validate_review_artifacts(study_root, manifest, records)
    if review_errors:
        raise ValueError("review artifact validation failed: " + "; ".join(review_errors))
    source_bytes_by_relative = _require_live_inventory_closure(source_root, records)
    prepared: dict[str, tuple[bytes, list[str]]] = {}
    expected_cache_names: set[str] = set()
    for record in records:
        document_id = _require_document_id(record.get("document_id"))
        source_bytes = source_bytes_by_relative[record["source_relative_path"]]
        _, pages, _ = _load_verified_ocr_cache(study_root, record)
        prepared[document_id] = (source_bytes, pages)
        expected_cache_names.update({f"{document_id}.txt", f"{document_id}.json"})
        if record.get("contains_report") is True:
            page_start = record.get("page_start")
            page_end = record.get("page_end")
            if (
                not isinstance(page_start, int)
                or not isinstance(page_end, int)
                or page_start < 1
                or page_end < page_start
                or page_end > len(pages)
            ):
                raise ValueError(f"{document_id} has no valid page range")
            output_relative = f"sections/{document_id}"
            for relative, field_name in (
                (output_relative, "section output directory"),
                (f"{output_relative}/loss-adjustment-report.pdf", "section PDF output path"),
                (f"{output_relative}/loss-adjustment-report.txt", "section text output path"),
                (f"{output_relative}/metadata.json", "section metadata output path"),
            ):
                _confined_path(
                    study_root,
                    relative,
                    field_name=field_name,
                    expected_relative=relative,
                )
        elif record.get("contains_report") is False:
            relative = f"no-report/{document_id}/metadata.json"
            _confined_path(
                study_root,
                relative,
                field_name="no-report output path",
                expected_relative=relative,
            )
        else:
            raise ValueError(f"{document_id}: unresolved OCR/classification")
    if _directory_entries(study_root / "_ocr_cache") != expected_cache_names:
        raise ValueError("OCR cache does not exactly close over manifest documents")
    working_root = _confined_path(
        study_root,
        "_working",
        field_name="working directory",
        expected_relative="_working",
    )
    if working_root.exists() and _directory_entries(working_root):
        raise ValueError("working directory contains stale runtime residue")
    working_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="extract-", dir=working_root))
    (staging_root / "sections").mkdir()
    (staging_root / "no-report").mkdir()

    def cleanup_staging() -> None:
        shutil.rmtree(staging_root, ignore_errors=True)
        try:
            working_root.rmdir()
        except OSError:
            pass

    def staged_call(callable_: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return callable_(*args, **kwargs)
        except Exception:
            cleanup_staging()
            raise

    extracted_count = 0
    no_report_metadata_count = 0
    for record in records:
        document_id = _require_document_id(record.get("document_id"))
        source_bytes, pages = prepared[document_id]
        if record["review_status"] != "verified":
            raise ValueError(
                f"{record['document_id']} is not verified; review manifest page ranges first"
            )
        if record["contains_report"] is False:
            metadata_relative = (
                Path("no-report") / record["document_id"] / "metadata.json"
            ).as_posix()
            metadata_path = _confined_path(
                staging_root,
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
            staged_call(atomic_write_json, metadata_path, metadata)
            record["outputs"] = {
                "metadata": metadata_path.relative_to(staging_root).as_posix()
            }
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
            staging_root,
            output_relative,
            field_name="section output directory",
            expected_relative=output_relative,
        )
        pdf_path = _confined_path(
            staging_root,
            f"{output_relative}/loss-adjustment-report.pdf",
            field_name="section PDF output path",
            expected_relative=f"{output_relative}/loss-adjustment-report.pdf",
        )
        text_path = _confined_path(
            staging_root,
            f"{output_relative}/loss-adjustment-report.txt",
            field_name="section text output path",
            expected_relative=f"{output_relative}/loss-adjustment-report.txt",
        )
        metadata_path = _confined_path(
            staging_root,
            f"{output_relative}/metadata.json",
            field_name="section metadata output path",
            expected_relative=f"{output_relative}/metadata.json",
        )
        range_metadata = staged_call(
            extract_pdf_range, source_bytes, pdf_path, page_start, page_end
        )
        metadata = {
            "document_id": record["document_id"],
            "source_path": record["source_path"],
            "source_relative_path": record["source_relative_path"],
            **range_metadata,
            "text_sha256": None,
        }
        staged_call(write_section_text, text_path, pages, page_start, page_end)
        metadata["text_sha256"] = staged_call(sha256_file, text_path)
        staged_call(atomic_write_json, metadata_path, metadata)
        record["outputs"] = {
            "directory": output_dir.relative_to(staging_root).as_posix(),
            "pdf": pdf_path.relative_to(staging_root).as_posix(),
            "text": text_path.relative_to(staging_root).as_posix(),
            "metadata": metadata_path.relative_to(staging_root).as_posix(),
        }
        extracted_count += 1
    manifest["extracted_report_count"] = extracted_count
    manifest["no_report_metadata_count"] = no_report_metadata_count

    def verify_commit_basis() -> None:
        _require_live_inventory_closure(source_root, records)
        review_errors = _validate_review_artifacts(study_root, manifest, records)
        if review_errors:
            raise ValueError("; ".join(review_errors))

    try:
        verify_commit_basis()
    except Exception:
        cleanup_staging()
        raise
    atomic_write_json(staging_root / "manifest.json", manifest)
    try:
        _publish_generation(
            study_root,
            staging_root,
            ["sections", "no-report", "manifest.json"],
            pre_commit_check=verify_commit_basis,
        )
    finally:
        if not (study_root / ".publication-journal.json").exists():
            cleanup_staging()
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
        boundary_bytes = _read_regular_file_no_follow(boundary_path)
        if hashlib.sha256(boundary_bytes).hexdigest() != reference.get("sha256"):
            raise ValueError("boundary review hash mismatch")
        boundary = json.loads(boundary_bytes.decode("utf-8"))
        if not isinstance(boundary, dict):
            raise ValueError("boundary review must be an object")
        if (
            boundary.get("document_count") != len(records)
            or reference.get("document_count") != len(records)
            or boundary.get("independent_review_status") != "verified"
        ):
            raise ValueError("boundary review coverage/status mismatch")
        decisions = boundary.get("decisions", [])
        if not isinstance(decisions, list) or not all(
            isinstance(decision, dict) for decision in decisions
        ):
            raise ValueError("boundary review decisions must be objects")
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
                    "ocr_sidecar_sha256",
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
        independent_bytes = _read_regular_file_no_follow(independent_path)
        if (
            hashlib.sha256(independent_bytes).hexdigest()
            != independent_reference.get("sha256")
        ):
            raise ValueError("independent boundary review hash mismatch")
        independent = json.loads(independent_bytes.decode("utf-8"))
        if not isinstance(independent, dict):
            raise ValueError("independent boundary review must be an object")
        primary_reviewer_id = str(boundary.get("primary_reviewer_id", "")).strip()
        independent_reviewer_id = str(independent.get("reviewer_id", "")).strip()
        if (
            not primary_reviewer_id
            or not independent_reviewer_id
            or primary_reviewer_id == independent_reviewer_id
        ):
            raise ValueError("reviewer identities must be nonempty and distinct")
        primary_generation_id = str(boundary.get("review_generation_id", "")).strip()
        independent_generation_id = str(
            independent.get("review_generation_id", "")
        ).strip()
        if (
            not primary_generation_id
            or not independent_generation_id
            or primary_generation_id == independent_generation_id
        ):
            raise ValueError(
                "review generation identities must be nonempty and distinct"
            )
        independent_decisions = independent.get("decisions", [])
        if not isinstance(independent_decisions, list) or not all(
            isinstance(decision, dict) for decision in independent_decisions
        ):
            raise ValueError("independent boundary review decisions must be objects")
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
                    "ocr_sidecar_sha256",
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


def _validate_study_unlocked(study_root: Path) -> dict[str, Any]:
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

    source_bytes_by_relative: dict[str, bytes] = {}
    try:
        source_bytes_by_relative = _require_live_inventory_closure(source_root, records)
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
            _resolve_manifest_source(source_root, record)
            source_bytes = source_bytes_by_relative[record["source_relative_path"]]
            if hashlib.sha256(source_bytes).hexdigest() != record.get("source_sha256"):
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
                if set(metadata) != set(expected_metadata) | {
                    "output_sha256",
                    "text_sha256",
                }:
                    raise ValueError("section metadata schema mismatch")
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
                with fitz.open(
                    stream=source_bytes, filetype="pdf"
                ) as source_pdf, fitz.open(pdf_path) as section_pdf:
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


def validate_study(study_root: Path) -> dict[str, Any]:
    try:
        study_root = _validate_study_root(study_root)
    except ValueError as exc:
        return {
            "document_count": 0,
            "report_count": 0,
            "no_report_count": 0,
            "errors": [str(exc)],
            "valid": False,
        }
    with _exclusive_study_lock(study_root) as anchored_root:
        return _validate_study_unlocked(anchored_root)


@_locked_study_mutation(0)
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
    live_inventory, source_bytes_by_relative = _source_inventory_snapshot(source_root)
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

    prepared: list[tuple[dict[str, Any], Path, bytes, dict[str, Any]]] = []
    for record in records:
        document_id = record.get("document_id", "<unknown>")
        try:
            document_id = _require_document_id(document_id)
            _resolve_manifest_source(source_root, record)
            source_bytes = source_bytes_by_relative[record["source_relative_path"]]
            if hashlib.sha256(source_bytes).hexdigest() != record.get("source_sha256"):
                raise ValueError("source hash mismatch")
            cache_path = _confined_path(
                resolved_study_root,
                record.get("ocr_cache_path"),
                field_name="ocr_cache_path",
                expected_relative=f"_ocr_cache/{document_id}.txt",
                require_exists=True,
            )
            sidecar_bytes = _read_regular_file_no_follow(cache_path)
            sidecar_text = sidecar_bytes.decode("utf-8")
            pages = split_sidecar_pages(
                sidecar_text, int(record.get("source_page_count", -1))
            )
            sidecar_hash = hashlib.sha256(sidecar_bytes).hexdigest()

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
                        _read_regular_file_no_follow(metadata_path)
                    )
                except (OSError, json.JSONDecodeError):
                    existing_metadata = {}
            recomputed_from_source = all(
                (
                    existing_metadata.get("schema_version") == "ocr_cache.v1",
                    existing_metadata.get("extraction_contract_sha256")
                    == OCR_EXTRACTION_CONTRACT_SHA256,
                    existing_metadata.get("document_id") == document_id,
                    existing_metadata.get("source_sha256")
                    == record.get("source_sha256"),
                    existing_metadata.get("source_page_count")
                    == record.get("source_page_count"),
                    existing_metadata.get("sidecar_sha256") == sidecar_hash,
                )
            )
            if not recomputed_from_source:
                raise ValueError(
                    "legacy cache lacks the current extraction contract; run refresh-cache"
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
                    _read_regular_file_no_follow(output_metadata_path)
                )
                pdf_bytes = _read_regular_file_no_follow(pdf_path)
                text_bytes = _read_regular_file_no_follow(text_path)
                if hashlib.sha256(pdf_bytes).hexdigest() != output_metadata.get(
                    "output_sha256"
                ):
                    raise ValueError("PDF hash mismatch")
                if hashlib.sha256(text_bytes).hexdigest() != output_metadata.get(
                    "text_sha256"
                ):
                    raise ValueError("text hash mismatch")
                page_start = int(record["page_start"])
                page_end = int(record["page_end"])
                with fitz.open(
                    stream=source_bytes, filetype="pdf"
                ) as source_pdf, fitz.open(
                    stream=pdf_bytes, filetype="pdf"
                ) as output_pdf:
                    if output_pdf.page_count != page_end - page_start + 1:
                        raise ValueError("PDF page count mismatch")
                    for offset, source_page_number in enumerate(
                        range(page_start - 1, page_end)
                    ):
                        if _page_render_sha256(source_pdf[source_page_number]) != _page_render_sha256(
                            output_pdf[offset]
                        ):
                            raise ValueError("selected source page content mismatch")
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
                    _read_regular_file_no_follow(output_metadata_path)
                )
                if output_metadata.get("source_sha256") != record.get("source_sha256"):
                    raise ValueError("no-report source hash mismatch")

            binding_basis = "recomputed_from_source"
            ocr_method = existing_metadata.get("ocr_method")
            cache_metadata = {
                "schema_version": "ocr_cache.v1",
                "extraction_contract_sha256": OCR_EXTRACTION_CONTRACT_SHA256,
                "document_id": document_id,
                "source_sha256": record["source_sha256"],
                "source_page_count": record["source_page_count"],
                "sidecar_sha256": sidecar_hash,
                "ocr_method": ocr_method,
                "binding_basis": binding_basis,
            }
            prepared.append((record, cache_path, sidecar_bytes, cache_metadata))
        except (KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
            preflight_errors.append(f"{document_id}: {exc}")

    if preflight_errors:
        raise ValueError(
            "reviewed cache provenance adoption failed: "
            + "; ".join(preflight_errors[:20])
        )

    working_root = _confined_path(
        resolved_study_root,
        "_working",
        field_name="working directory",
        expected_relative="_working",
    )
    if working_root.exists() and _directory_entries(working_root):
        raise ValueError("working directory contains stale runtime residue")
    working_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="adopt-cache-", dir=working_root))
    staged_cache_root = staging_root / "_ocr_cache"
    staged_cache_root.mkdir()

    def cleanup_staging() -> None:
        shutil.rmtree(staging_root, ignore_errors=True)
        try:
            working_root.rmdir()
        except OSError:
            pass

    for record, _, sidecar_bytes, cache_metadata in prepared:
        document_id = record["document_id"]
        (staged_cache_root / f"{document_id}.txt").write_bytes(sidecar_bytes)
        atomic_write_json(staged_cache_root / f"{document_id}.json", cache_metadata)
        record["ocr_method"] = cache_metadata["ocr_method"]
        record["ocr_sidecar_sha256"] = cache_metadata["sidecar_sha256"]
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
    atomic_write_json(staging_root / "manifest.json", manifest)

    def verify_commit_basis() -> None:
        _require_live_inventory_closure(source_root, records)
        review_errors = _validate_review_artifacts(
            resolved_study_root, manifest, records
        )
        if review_errors:
            raise ValueError("; ".join(review_errors))
        for record, cache_path, sidecar_bytes, _ in prepared:
            if _read_regular_file_no_follow(cache_path) != sidecar_bytes:
                raise ValueError(
                    f"{record['document_id']}: OCR sidecar changed before adoption commit"
                )

    try:
        _publish_generation(
            resolved_study_root,
            staging_root,
            ["_ocr_cache", "manifest.json"],
            pre_commit_check=verify_commit_basis,
        )
    finally:
        if not (resolved_study_root / ".publication-journal.json").exists():
            cleanup_staging()
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
