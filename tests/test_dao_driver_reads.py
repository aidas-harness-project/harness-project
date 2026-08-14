"""DAO-owned redacted-text and evidence surfaces for stage drivers."""
import hashlib
import json
from pathlib import Path

import dao


CASE = "CASE_009"
DOC = "DOC_001"
TEXT = "<<<PAGE page=1>>>\nA redacted driver source.\n<<<PAGE page=2>>>\nSecond page.\n"


def _seed(isolated_dao, *, disposition="automated_text_pipeline"):
    case_dir = isolated_dao / "outputs" / CASE
    case_dir.mkdir(parents=True, exist_ok=True)
    dao.atomic_write_json(case_dir / "document_manifest.json", {
        "case_id": CASE,
        "documents": [{
            "document_id": DOC,
            "file_name": "source.pdf",
            "source_file_name": "source.pdf",
            "source_page_start": 1,
            "source_page_end": 2,
            "document_type": "insurance_policy",
            "downstream_disposition": disposition,
        }],
    })
    processed = isolated_dao / "data" / "processed" / CASE / DOC
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "redacted_text.md").write_text(TEXT, encoding="utf-8")
    dao.atomic_write_json(case_dir / "_revision_index.json", {
        "case_id": CASE,
        "documents": [{
            "document_id": DOC,
            "current_revision_sha256": "revision-1",
            "uid_scheme": "legacy",
        }],
    })


def test_redacted_bundle_returns_content_not_a_processed_path(isolated_dao, make_args, capsys):
    _seed(isolated_dao)

    assert dao.cmd_read_redacted_text_bundle(make_args(case_id=CASE, doc_id=[DOC])) == 0
    payload = json.loads(capsys.readouterr().out)
    document = payload["documents"][0]

    assert document["document_id"] == DOC
    assert document["source_file_name"] == "source.pdf"
    assert document["source_text_revision_sha256"] == "revision-1"
    assert document["pages"] == [
        {"page": 1, "text": "A redacted driver source.\n"},
        {"page": 2, "text": "Second page.\n"},
    ]
    assert "redacted_text.md" not in json.dumps(payload)
    assert document["redacted_text_sha256"] == hashlib.sha256(
        TEXT.encode("utf-8")).hexdigest()


def test_redacted_bundle_refuses_superseded_and_expert_review_documents(
    isolated_dao, make_args, capsys
):
    for disposition, expected in [
        ("superseded_bundle", "SUPERSEDED_BUNDLE"),
        ("expert_review_only", "NON_TEXT_EXPERT_REVIEW_ONLY"),
    ]:
        _seed(isolated_dao, disposition=disposition)
        assert dao.cmd_read_redacted_text_bundle(make_args(case_id=CASE, doc_id=[DOC])) == 1
        assert expected in capsys.readouterr().out


def test_evidence_verifier_returns_current_bindings_and_checks_exact_range(
    isolated_dao, make_args, capsys, tmp_path
):
    _seed(isolated_dao)
    page = "A redacted driver source.\n"
    quote = "redacted driver"
    start = page.index(quote)
    references_file = tmp_path / "references.json"
    references_file.write_text(json.dumps({"references": [{
        "document_id": DOC,
        "page": 1,
        "quote": quote,
        "start_char": start,
        "end_char": start + len(quote),
        "source_text_revision_sha256": "revision-1",
    }]}), encoding="utf-8")

    assert dao.cmd_verify_evidence_references(make_args(
        case_id=CASE, references_file=str(references_file)
    )) == 0
    verified = json.loads(capsys.readouterr().out)["verified_references"][0]
    assert verified["source_text_revision_sha256"] == "revision-1"
    assert verified["start_char"] == start
    assert verified["redacted_text_sha256"]


def test_evidence_verifier_refuses_stale_revision_and_wrong_quote_range(
    isolated_dao, make_args, capsys, tmp_path
):
    _seed(isolated_dao)
    for reference, expected in [
        ({"document_id": DOC, "page": 1, "quote": "driver",
          "source_text_revision_sha256": "old-revision"}, "stale"),
        ({"document_id": DOC, "page": 1, "quote": "driver",
          "start_char": 0, "end_char": 6}, "exactly match"),
    ]:
        references_file = tmp_path / f"{expected}.json"
        references_file.write_text(json.dumps({"references": [reference]}), encoding="utf-8")
        assert dao.cmd_verify_evidence_references(make_args(
            case_id=CASE, references_file=str(references_file)
        )) == 1
        assert expected in capsys.readouterr().out
