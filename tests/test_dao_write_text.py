"""dao.py's write-text (generic locked+atomic text write to outputs/) and
write-reviewed-draft (critic's wrapper around it) -- closes the gap found
in the end-to-end pipeline review: critic's draft_report_v{version}_reviewed.md
had no write path at all before this.
"""
import pytest

import dao


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


def _text_file(tmp_path, content):
    p = tmp_path / "annotated.md"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_write_text_writes_arbitrary_filename_and_releases_lock(isolated_dao, make_args, tmp_path):
    text_file = _text_file(tmp_path, "## Annotated draft\n\nSome content [critic note: hedge this].")
    args = make_args(filename="some_freeform_note.md", text_file=text_file)

    rc = dao.cmd_write_text(args)

    target = isolated_dao / "outputs" / "CASE_009" / "some_freeform_note.md"
    assert rc == 0
    assert target.read_text(encoding="utf-8").startswith("## Annotated draft")
    assert not target.with_name(target.name + ".lock").exists()


def test_write_text_rejects_when_locked(isolated_dao, make_args, tmp_path):
    target = isolated_dao / "outputs" / "CASE_009" / "note.md"
    dao.acquire_lock(target, "someone-else", "RUN_OTHER", "holding")
    text_file = _text_file(tmp_path, "content")
    args = make_args(filename="note.md", text_file=text_file)

    rc = dao.cmd_write_text(args)

    assert rc == 1
    assert not target.exists()


def test_write_reviewed_draft_uses_the_fixed_filename_convention(isolated_dao, make_args, tmp_path):
    text_file = _text_file(tmp_path, "annotated draft v1 content")
    args = make_args(version="v1", text_file=text_file)

    rc = dao.cmd_write_reviewed_draft(args)

    target = isolated_dao / "outputs" / "CASE_009" / "draft_report_v1_reviewed.md"
    assert rc == 0
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "annotated draft v1 content"


def test_write_reviewed_draft_v1_and_v2_do_not_collide(isolated_dao, make_args, tmp_path):
    v1_file = _text_file(tmp_path, "v1 content")
    v2_file = tmp_path / "v2.md"
    v2_file.write_text("v2 content", encoding="utf-8")

    dao.cmd_write_reviewed_draft(make_args(version="v1", text_file=v1_file))
    dao.cmd_write_reviewed_draft(make_args(version="v2", text_file=str(v2_file)))

    v1_target = isolated_dao / "outputs" / "CASE_009" / "draft_report_v1_reviewed.md"
    v2_target = isolated_dao / "outputs" / "CASE_009" / "draft_report_v2_reviewed.md"
    assert v1_target.read_text(encoding="utf-8") == "v1 content"
    assert v2_target.read_text(encoding="utf-8") == "v2 content"


def test_write_reviewed_draft_rejects_invalid_version(isolated_dao, make_args, tmp_path):
    """CLI-level enforcement is argparse's choices=["v1","v2"] -- but
    cmd_write_reviewed_draft is also called directly in these tests
    (bypassing argparse), so it needs its own defense-in-depth check too."""
    text_file = _text_file(tmp_path, "content")
    args = make_args(version="v3", text_file=text_file)

    with pytest.raises(SystemExit):
        dao.cmd_write_reviewed_draft(args)


def test_read_page_text_returns_only_validated_processed_page(isolated_dao, make_args, capsys):
    page_path = isolated_dao / "data" / "processed" / "CASE_009" / "DOC_001" / "page_001.md"
    page_path.parent.mkdir(parents=True)
    page_path.write_text("검증된 페이지 텍스트", encoding="utf-8")

    rc = dao.cmd_read_page_text(make_args(page=1, caller_stage="document-pipeline"))

    assert rc == 0
    assert capsys.readouterr().out == "검증된 페이지 텍스트"


def test_read_page_text_fails_when_checkpoint1_page_is_missing(isolated_dao, make_args, capsys):
    rc = dao.cmd_read_page_text(make_args(page=3, caller_stage="document-pipeline"))

    assert rc == 1
    assert "NOT_EXTRACTED" in capsys.readouterr().out


def test_read_document_text_reports_non_text_instead_of_requesting_reextraction(
    isolated_dao, make_args, capsys
):
    manifest_path = isolated_dao / "outputs" / "CASE_009" / "document_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    dao.atomic_write_json(manifest_path, {
        "case_id": "CASE_009",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_009/DOC_001.pdf",
            "file_format": "pdf",
            "file_size_bytes": 1,
            "ocr_status": "not_applicable",
            "downstream_disposition": "expert_review_only",
        }],
    })

    rc = dao.cmd_read_document_text(make_args())

    assert rc == 1
    assert "NON_TEXT_EXPERT_REVIEW_ONLY" in capsys.readouterr().out


@pytest.mark.parametrize("page", [0, -1])
def test_read_page_text_rejects_non_positive_page_numbers(isolated_dao, make_args, capsys, page):
    unexpected_path = isolated_dao / "data" / "processed" / "CASE_009" / "DOC_001" / f"page_{page:03d}.md"
    unexpected_path.parent.mkdir(parents=True)
    unexpected_path.write_text("must not be read", encoding="utf-8")

    rc = dao.cmd_read_page_text(make_args(page=page, caller_stage="document-pipeline"))

    assert rc == 1
    assert capsys.readouterr().out == f"ERROR: page must be >= 1 (got {page})\n"
