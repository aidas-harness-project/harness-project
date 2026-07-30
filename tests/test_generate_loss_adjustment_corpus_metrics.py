import csv
from pathlib import Path

import pytest

import generate_loss_adjustment_corpus_metrics as metrics  # pyright: ignore[reportMissingImports]


def test_build_metrics_counts_each_defined_phrase_at_most_once_per_document(
    tmp_path: Path,
):
    text_dir = tmp_path / "sections/DOC_001"
    text_dir.mkdir(parents=True)
    (text_dir / "loss-adjustment-report.txt").write_text(
        "보험금 사정 요약 보험금 사정 요약\n"
        "관계법규와 보험약관에 의하면 위 금액이 적정하다고 판단됩니다.\n"
        "증빙자료는 별첨하며 향후 금액은 달라질 수 있어 판단 유보함.\n",
        encoding="utf-8",
    )
    manifest = {
        "documents": [
            {
                "document_id": "DOC_001",
                "source_relative_path": "POC/report.pdf",
                "source_page_count": 10,
                "source_size_bytes": 100,
                "contains_report": True,
                "page_start": 2,
                "page_end": 4,
                "review_status": "verified",
                "outputs": {
                    "text": "sections/DOC_001/loss-adjustment-report.txt"
                },
            },
            {
                "document_id": "DOC_002",
                "source_relative_path": "POC/notice.pdf",
                "source_page_count": 1,
                "source_size_bytes": 10,
                "contains_report": False,
                "page_start": None,
                "page_end": None,
                "review_status": "verified",
                "outputs": {"metadata": "no-report/DOC_002/metadata.json"},
            },
        ]
    }

    result = metrics.build_metrics(
        manifest,
        tmp_path,
        manifest_sha256="a" * 64,
        report_index_sha256="b" * 64,
        family_assignments={"DOC_001": "disease_benefit", "DOC_002": "disease_benefit"},
    )

    assert result["families"]["poc_mixed"]["source_document_count"] == 0
    family = result["families"]["disease_benefit"]
    assert family["source_document_count"] == 2
    assert family["report_count"] == 1
    assert family["no_report_count"] == 1
    assert family["report_pages_total"] == 3
    assert family["component_document_counts_lower_bound"]["adjustment_summary"] == 1
    assert family["component_document_counts_lower_bound"]["governing_law_or_policy"] == 1
    assert family["tone_document_counts_lower_bound"]["deemed_appropriate"] == 1
    assert family["tone_document_counts_lower_bound"]["reservation"] == 1
    assert family["tone_document_counts_lower_bound"]["may_change"] == 1
    assert result["generator"]["component_patterns"] == metrics.COMPONENT_PATTERNS
    assert result["generator"]["report_index_sha256"] == "b" * 64


def test_load_report_index_requires_exact_manifest_metadata(tmp_path: Path):
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    manifest = {
        "documents": [
            {
                "document_id": "DOC_001",
                "source_relative_path": "arbitrary/source.pdf",
                "source_page_count": 3,
                "contains_report": True,
                "page_start": 1,
                "page_end": 2,
                "review_status": "verified",
            }
        ]
    }
    row = {
        "document_id": "DOC_001",
        "source_relative_path": "arbitrary/source.pdf",
        "family": "liability_damages",
        "contains_report": "True",
        "source_page_count": "3",
        "report_page_start": "1",
        "report_page_end": "2",
        "report_page_count": "2",
        "review_status": "verified",
        "section_directory": "sections/DOC_001",
    }
    index_path = analysis / "report-index.csv"
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=metrics.REPORT_INDEX_FIELDS)
        writer.writeheader()
        writer.writerow(row)

    assignments, loaded_path = metrics.load_report_index(manifest, tmp_path)
    assert assignments == {"DOC_001": "liability_damages"}
    assert loaded_path == index_path

    row["report_page_end"] = "3"
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=metrics.REPORT_INDEX_FIELDS)
        writer.writeheader()
        writer.writerow(row)
    with pytest.raises(ValueError, match="report index metadata mismatch"):
        metrics.load_report_index(manifest, tmp_path)


def test_metrics_cli_has_no_arbitrary_output_path(tmp_path: Path):
    with pytest.raises(SystemExit):
        metrics._parser().parse_args(
            [str(tmp_path / "study"), "--output", str(tmp_path / "metrics.json")]
        )
