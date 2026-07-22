"""The redaction boundary: which DAO read path is safe for which stage.

Found during the CASE_901 run. `denial-response` called `read-document-text`,
got back a one-line *path* where its spec had promised text, concluded the
command had failed, and fell back to `read-page-text` -- which serves
checkpoint 1 output, before redaction. It read the claimant-facing PII that
checkpoint 2 exists to remove (a 대표이사's name, a street address, phone and
fax numbers). Nothing structural stopped it; no PII happened to reach the
output's evidence quotes, which was luck, not design.

The specs now say so explicitly, but a spec is a prompt. These tests pin the
part that can actually be enforced: the two commands are NOT interchangeable,
and what separates them is exactly the redaction pass.

The PII strings below are the real ones from CASE_901/DOC_001, kept verbatim
so a regression reproduces the original finding rather than a paraphrase of it.
"""
import pytest

import dao


PRE_REDACTION_PAGE = """\
※ 소비자정책팀 : 서울시 서초구 서초대로74길 14, 33층 소비자보호파트(우 : 06620)
※ 상담전화: (02)758-7755   팩스: (02)758-7766
대표이사    나 채 범
"""

REDACTED_PAGE = """\
※ 소비자정책팀 : [ADDRESS]
※ 상담전화: [PHONE_NUMBER]   팩스: [PHONE_NUMBER]
대표이사    [PERSON_NAME]
"""

PII_VALUES = [
    "서울시 서초구 서초대로74길 14",
    "(02)758-7755",
    "(02)758-7766",
    "나 채 범",
]


@pytest.fixture
def processed_document(isolated_dao):
    """A document that has cleared both checkpoints, as the pipeline leaves it.

    checkpoint 1 -> page_NNN.md (pre-redaction)
    checkpoint 2 -> redacted_text.md (post-redaction, page-marked)
    """
    processed = isolated_dao / "data" / "processed" / "CASE_009" / "DOC_001"
    processed.mkdir(parents=True)
    (processed / "page_001.md").write_text(PRE_REDACTION_PAGE, encoding="utf-8")
    (processed / "redacted_text.md").write_text(
        f"<<<PAGE page=1>>>\n{REDACTED_PAGE}", encoding="utf-8"
    )
    return processed


def test_page_text_is_pre_redaction_and_redacted_text_is_not(processed_document):
    """The invariant the whole boundary rests on.

    If a change ever makes checkpoint 1's page files carry redacted content --
    or checkpoint 2's output carry raw content -- the distinction the specs
    draw between these two read paths stops meaning anything.
    """
    raw = (processed_document / "page_001.md").read_text(encoding="utf-8")
    redacted = (processed_document / "redacted_text.md").read_text(encoding="utf-8")

    for pii in PII_VALUES:
        assert pii in raw, f"{pii!r} should be present pre-redaction"
        assert pii not in redacted, f"{pii!r} leaked past redaction"


def test_read_page_text_returns_pre_redaction_content(
    processed_document, make_args, capsys
):
    """read-page-text hands back raw text -- correct for its one caller
    (redact_document.py, which feeds it straight to the Redactor) and exactly
    why no analysis stage may use it."""
    rc = dao.cmd_read_page_text(make_args(page=1))

    out = capsys.readouterr().out
    assert rc == 0
    assert "나 채 범" in out
    assert "[PERSON_NAME]" not in out


def test_read_document_text_returns_a_path_not_the_text(
    processed_document, make_args, capsys
):
    """The contract that got misread during CASE_901.

    A one-line path IS the success case here -- callers read that path. This
    test exists so nobody "fixes" the command into printing text on the theory
    that the path return was the bug.
    """
    rc = dao.cmd_read_document_text(make_args())

    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.endswith("redacted_text.md")
    assert "소비자정책팀" not in out, "the command must not emit document content"


def test_the_path_read_document_text_returns_holds_redacted_content(
    processed_document, make_args, capsys
):
    """Following the documented path must land an analysis stage on redacted
    text -- the actual end-to-end guarantee for denial-response/policy-pipeline."""
    dao.cmd_read_document_text(make_args())
    path = capsys.readouterr().out.strip()

    content = dao.Path(path).read_text(encoding="utf-8")

    assert "[PERSON_NAME]" in content
    for pii in PII_VALUES:
        assert pii not in content


def test_the_two_commands_are_not_interchangeable(
    processed_document, make_args, capsys
):
    """One returns a path, the other returns text, and only one of the two is
    redacted. Any change collapsing that difference re-opens the bypass."""
    dao.cmd_read_document_text(make_args())
    document_out = capsys.readouterr().out
    dao.cmd_read_page_text(make_args(page=1))
    page_out = capsys.readouterr().out

    assert document_out != page_out
    assert not any(pii in document_out for pii in PII_VALUES)
    assert any(pii in page_out for pii in PII_VALUES)


def test_expert_review_only_document_is_refused_before_any_path_is_emitted(
    isolated_dao, make_args, capsys
):
    """The gate still holds for non-text visual evidence: a blocked document
    must not yield a path an agent could open (P8's expert_review_only route)."""
    processed = isolated_dao / "data" / "processed" / "CASE_009" / "DOC_001"
    processed.mkdir(parents=True)
    (processed / "redacted_text.md").write_text("should never be reachable", encoding="utf-8")

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

    out = capsys.readouterr().out
    assert rc == 1
    assert "NON_TEXT_EXPERT_REVIEW_ONLY" in out
    assert "redacted_text.md" not in out
