#!/usr/bin/env python3
"""Generate reproducible family, page, component, and tone metrics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any

import extract_loss_adjustment_sections as extractor

FAMILY_ORDER = (
    "poc_mixed",
    "automobile_ta",
    "personal_accident_benefit",
    "liability_damages",
    "disease_benefit",
)

REPORT_INDEX_FIELDS = (
    "document_id",
    "source_relative_path",
    "source_sha256",
    "source_size_bytes",
    "family",
    "contains_report",
    "source_page_count",
    "report_page_start",
    "report_page_end",
    "report_page_count",
    "review_status",
    "section_directory",
)

LEGACY_REPORT_INDEX_FIELDS = tuple(
    field
    for field in REPORT_INDEX_FIELDS
    if field not in {"source_sha256", "source_size_bytes"}
)

COMPONENT_PATTERNS = {
    "submission_letter": [
        r"(?:손해사정서|보험금\s*사정서).{0,100}(?:제출|송부)",
        r"(?:제출|송부)(?:합니다|드립니다)",
    ],
    "adjustment_summary": [
        r"사정\s*(?:요약|결과)",
        r"보험금\s*사정\s*요약",
        r"손해액\s*요약",
    ],
    "assignment_and_contract": [
        r"손해사정\s*(?:위임|수임)",
        r"위임\s*및\s*보험계약",
        r"(?:수임|위임)\s*정보",
        r"위임\s*계약",
    ],
    "accident_or_claim_facts": [
        r"사고\s*(?:개요|경위|내용|사실)",
        r"사고\s*및\s*손해\s*발생",
        r"보험\s*사고",
        r"청구\s*사실",
        r"질병\s*발생",
    ],
    "governing_law_or_policy": [
        r"(?:관계|관련)\s*법규",
        r"적용\s*약관",
        r"보험\s*약관",
        r"보상\s*책임",
        r"지급\s*책임",
    ],
    "quantification": [
        r"사정\s*금액",
        r"손해액\s*(?:산정|계산)",
        r"보험금\s*(?:사정|산정)",
        r"지급\s*보험금",
        r"손해\s*배상금",
    ],
    "final_opinion": [
        r"사정\s*(?:결과\s*및\s*)?의견",
        r"최종\s*의견",
        r"조사\s*의견",
        r"결\s*론",
    ],
    "reservation_or_variability": [
        r"(?:판단|사정)\s*유보",
        r"유보(?:함|합니다)",
        r"달라질\s*수",
        r"변동될\s*수",
        r"추가\s*손해",
    ],
    "evidence_list": [
        r"증빙\s*자료",
        r"첨부\s*자료",
        r"입증\s*자료",
        r"제출\s*서류",
    ],
}

TONE_PATTERNS = {
    "evidence_basis": [
        r"근거(?:로|하여)",
        r"기초(?:로|하여)",
        r"에\s*의하면",
        r"참조(?:로|하여)",
    ],
    "attachment_pointer": [r"별첨", r"첨부", r"증빙\s*자료", r"붙임"],
    "deemed_appropriate": [r"(?:타당|적정)하다고\s*판단"],
    "no_applicable_exclusion": [
        r"면책.{0,40}(?:없|해당하지)",
        r"해당\s*사항\s*없",
    ],
    "payment_liability_arises": [
        r"지급\s*책임.{0,30}(?:발생|있)",
        r"보험금.{0,30}지급.{0,30}책임",
    ],
    "reservation": [r"유보(?:함|합니다)", r"판단\s*유보", r"사정\s*유보"],
    "may_change": [r"달라질\s*수", r"변동될\s*수"],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_ocr_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value))


def _matches_any(value: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def _confined_text_path(study_root: Path, relative: Any, document_id: str) -> Path:
    expected = f"sections/{document_id}/loss-adjustment-report.txt"
    if relative != expected:
        raise ValueError(f"{document_id}: noncanonical report text path")
    root = study_root.resolve(strict=True)
    path = (root / expected).resolve(strict=True)
    if not path.is_relative_to(root) or path.is_symlink():
        raise ValueError(f"{document_id}: report text escapes study root")
    return path


def _report_index_path(study_root: Path) -> Path:
    return extractor._confined_path(
        study_root,
        "analysis/report-index.csv",
        field_name="report index path",
        expected_relative="analysis/report-index.csv",
        require_exists=True,
    )


def _expected_index_metadata(
    document_id: str, record: dict[str, Any]
) -> dict[str, Any]:
    contains_report = record.get("contains_report") is True
    page_start = record.get("page_start")
    page_end = record.get("page_end")
    if contains_report and (
        not isinstance(page_start, int)
        or isinstance(page_start, bool)
        or not isinstance(page_end, int)
        or isinstance(page_end, bool)
    ):
        raise ValueError(f"{document_id}: invalid reviewed report range")
    if contains_report:
        assert isinstance(page_start, int) and isinstance(page_end, int)
        report_page_start = str(page_start)
        report_page_end = str(page_end)
        report_page_count = str(page_end - page_start + 1)
    else:
        report_page_start = ""
        report_page_end = ""
        report_page_count = "0"
    return {
        "document_id": document_id,
        "source_relative_path": record.get("source_relative_path"),
        "source_sha256": record.get("source_sha256"),
        "source_size_bytes": str(record.get("source_size_bytes")),
        "contains_report": str(contains_report),
        "source_page_count": str(record.get("source_page_count")),
        "report_page_start": report_page_start,
        "report_page_end": report_page_end,
        "report_page_count": report_page_count,
        "review_status": record.get("review_status"),
        "section_directory": f"sections/{document_id}" if contains_report else "",
    }


def _manifest_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = manifest.get("documents", [])
    if not isinstance(records, list):
        raise ValueError("manifest documents must be an array")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("manifest documents must contain objects")
        document_id = record.get("document_id")
        if not isinstance(document_id, str) or document_id in result:
            raise ValueError("manifest must contain unique string document IDs")
        result[document_id] = record
    return result


def migrate_report_index(manifest: dict[str, Any], study_root: Path) -> bool:
    """Add manifest-bound identity fields to an exact legacy report index.

    Returns ``True`` when the file changed and ``False`` when it was already
    canonical. Unknown shapes fail before the file is touched.
    """

    index_path = _report_index_path(study_root)
    with index_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = tuple(reader.fieldnames or ())
        rows = list(reader)
    if headers == REPORT_INDEX_FIELDS:
        return False
    if headers != LEGACY_REPORT_INDEX_FIELDS:
        raise ValueError("report index headers are neither legacy nor canonical")

    manifest_by_id = _manifest_by_id(manifest)
    row_by_id = {row.get("document_id"): row for row in rows}
    if len(row_by_id) != len(rows) or set(row_by_id) != set(manifest_by_id):
        raise ValueError("report index must exactly cover unique manifest documents")

    migrated: list[dict[str, Any]] = []
    for document_id, record in manifest_by_id.items():
        row = row_by_id[document_id]
        family = row.get("family")
        if family not in FAMILY_ORDER:
            raise ValueError(f"{document_id}: invalid reviewed family assignment")
        expected = _expected_index_metadata(document_id, record)
        legacy_expected = {
            field: value
            for field, value in expected.items()
            if field in LEGACY_REPORT_INDEX_FIELDS and field != "family"
        }
        if any(row.get(field) != value for field, value in legacy_expected.items()):
            raise ValueError(f"{document_id}: report index metadata mismatch")
        migrated.append({**expected, "family": family})

    temporary = index_path.with_suffix(index_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(migrated)
    temporary.replace(index_path)
    return True


def load_report_index(
    manifest: dict[str, Any], study_root: Path
) -> tuple[dict[str, str], Path, str]:
    index_path = _report_index_path(study_root)
    index_bytes = index_path.read_bytes()
    with io.StringIO(index_bytes.decode("utf-8"), newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REPORT_INDEX_FIELDS:
            raise ValueError("report index headers do not match the canonical schema")
        rows = list(reader)

    records = manifest.get("documents", [])
    manifest_by_id = _manifest_by_id(manifest)
    row_by_id = {row.get("document_id"): row for row in rows}
    if (
        len(manifest_by_id) != len(records)
        or len(row_by_id) != len(rows)
        or set(row_by_id) != set(manifest_by_id)
    ):
        raise ValueError("report index must exactly cover unique manifest documents")

    assignments: dict[str, str] = {}
    for document_id, record in manifest_by_id.items():
        row = row_by_id[document_id]
        family = row.get("family")
        if family not in FAMILY_ORDER:
            raise ValueError(f"{document_id}: invalid reviewed family assignment")
        expected = _expected_index_metadata(document_id, record)
        if any(row.get(field) != value for field, value in expected.items()):
            raise ValueError(f"{document_id}: report index metadata mismatch")
        assignments[document_id] = family
    return assignments, index_path, hashlib.sha256(index_bytes).hexdigest()


def build_metrics(
    manifest: dict[str, Any],
    study_root: Path,
    *,
    manifest_sha256: str,
    report_index_sha256: str,
    family_assignments: dict[str, str],
) -> dict[str, Any]:
    family_records: dict[str, list[dict[str, Any]]] = {
        family: [] for family in FAMILY_ORDER
    }
    for record in manifest.get("documents", []):
        family_records[family_assignments[record["document_id"]]].append(record)

    families: dict[str, Any] = {}
    for family in FAMILY_ORDER:
        records = family_records[family]
        reports = [record for record in records if record.get("contains_report") is True]
        page_counts = [
            record["page_end"] - record["page_start"] + 1 for record in reports
        ]
        component_counts = {key: 0 for key in COMPONENT_PATTERNS}
        tone_counts = {key: 0 for key in TONE_PATTERNS}
        for record in reports:
            text_path = _confined_text_path(
                study_root, record.get("outputs", {}).get("text"), record["document_id"]
            )
            text = normalize_ocr_text(text_path.read_text(encoding="utf-8"))
            for key, patterns in COMPONENT_PATTERNS.items():
                component_counts[key] += int(_matches_any(text, patterns))
            for key, patterns in TONE_PATTERNS.items():
                tone_counts[key] += int(_matches_any(text, patterns))
        families[family] = {
            "source_document_count": len(records),
            "report_count": len(reports),
            "no_report_count": len(records) - len(reports),
            "report_pages_total": sum(page_counts),
            "report_pages_min": min(page_counts) if page_counts else None,
            "report_pages_median": statistics.median(page_counts) if page_counts else None,
            "report_pages_max": max(page_counts) if page_counts else None,
            "component_document_counts_lower_bound": component_counts,
            "tone_document_counts_lower_bound": tone_counts,
        }

    records = manifest.get("documents", [])
    reports = [record for record in records if record.get("contains_report") is True]
    return {
        "schema_version": "loss_adjustment_corpus_metrics.v0.2",
        "method_note": (
            "Phrase/component counts are per-document OCR-derived lower bounds. "
            "Family groups come from the reviewed analysis/report-index.csv assignments. "
            "Each count increments once when any listed regex matches NFC-normalized, "
            "Unicode-whitespace-removed text from the reviewed extracted report slice. "
            "These are reproducible regex-signal counts, not independently adjudicated "
            "ground-truth labels for semantic component presence."
        ),
        "generator": {
            "path": "tools/generate_loss_adjustment_corpus_metrics.py",
            "manifest_sha256": manifest_sha256,
            "report_index_path": "analysis/report-index.csv",
            "report_index_sha256": report_index_sha256,
            "component_patterns": COMPONENT_PATTERNS,
            "tone_patterns": TONE_PATTERNS,
        },
        "source_document_count": len(records),
        "source_pages_total": sum(record["source_page_count"] for record in records),
        "source_bytes_total": sum(record["source_size_bytes"] for record in records),
        "report_count": len(reports),
        "no_report_count": len(records) - len(reports),
        "report_pages_total": sum(
            record["page_end"] - record["page_start"] + 1 for record in reports
        ),
        "all_reviewed": all(
            record.get("review_status") == "verified" for record in records
        ),
        "families": families,
    }


def generate(study_root: Path) -> dict[str, Any]:
    validation = extractor.validate_study(study_root)
    if not validation["valid"]:
        raise ValueError(
            "study validation failed before metrics generation: "
            + "; ".join(validation["errors"][:10])
        )
    manifest_path, _, _, study_root = extractor._load_study_manifest(study_root)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    family_assignments, report_index_path, report_index_sha256 = load_report_index(
        manifest, study_root
    )
    result = build_metrics(
        manifest,
        study_root,
        manifest_sha256=manifest_sha256,
        report_index_sha256=report_index_sha256,
        family_assignments=family_assignments,
    )
    if sha256_file(manifest_path) != manifest_sha256:
        raise ValueError("manifest changed during metrics generation")
    if sha256_file(report_index_path) != report_index_sha256:
        raise ValueError("report index changed during metrics generation")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study_root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--migrate-index", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.migrate_index:
        manifest_path, _, _, study_root = extractor._load_study_manifest(
            args.study_root
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed = migrate_report_index(manifest, study_root)
        print(
            "migrated report index to canonical headers"
            if changed
            else "report index already uses canonical headers"
        )
        return 0
    output = args.study_root / "analysis/corpus-metrics.json"
    metrics = generate(args.study_root)
    rendered = json.dumps(metrics, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            print(f"metrics are stale: {output}")
            return 1
        print(f"metrics are current: {output}")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
