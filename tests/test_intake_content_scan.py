"""intake_case.py's content pre-check (known-gaps.md item 2, fixing intake's
content-blind classification -- the CASE_002 incident: DOC_002/DOC_003
looked like plain claim documents by filename but were actually completed
third-party loss-adjustment reports with stated payout figures).

_parse_content_scan_verdict is pure and tested directly (no real claude CLI
or page images needed). build_ledger's wiring is tested with fake plan
entries -- no real PDF needed there either.
"""
from types import SimpleNamespace

import pytest

import intake_case


@pytest.mark.parametrize("response,expected_flagged", [
    ("FLAGGED: title page reads 보험금사정서, 손해사정사 stamp visible", True),
    ("FLAGGED: 사정 결과 및 의견 section states a 20,000,000원 payout", True),
    ("CLEAR", False),
    ("CLEAR -- this looks like an ordinary diagnosis certificate", False),
    ("The document appears CLEAR of any adjustment conclusions.", False),
])
def test_parse_content_scan_verdict_recognized_tokens(response, expected_flagged):
    result = intake_case._parse_content_scan_verdict(response)
    assert result["flagged"] == expected_flagged


def test_parse_content_scan_verdict_unparseable_fails_safe_to_flagged():
    """Ambiguous/garbled model output must default to flagged=True -- this
    is a safety check, not a productivity one. Silently waving a file
    through on a parse failure is exactly the CASE_002 failure mode again,
    just moved one layer down."""
    result = intake_case._parse_content_scan_verdict("completely garbled nonsense")
    assert result["flagged"] is True
    assert "garbled nonsense" in result["evidence"]


def test_parse_content_scan_verdict_flagged_wins_if_both_tokens_present():
    """Defensive: if a response somehow contains both tokens (e.g. echoing
    the prompt's own instructions), treat it as flagged -- fail toward
    caution, never toward silently clearing a file."""
    result = intake_case._parse_content_scan_verdict(
        "Reply with exactly one line: FLAGGED: ... or CLEAR. FLAGGED: matches the pattern."
    )
    assert result["flagged"] is True


class FakeFile:
    def __init__(self, name):
        self.name = name


def test_build_ledger_only_flagged_files_get_content_warning():
    plan = [
        (FakeFile("a.pdf"), "raw"),
        (FakeFile("b.pdf"), "raw"),
        (FakeFile("c.pdf"), "ground_truth"),
    ]
    warnings = {"a.pdf": {"flagged": True, "evidence": "FLAGGED: test evidence", "pages_checked": 5}}

    ledger = intake_case.build_ledger("CASE_009", "src", plan, {}, warnings)

    by_name = {f["file_name"]: f for f in ledger["files"]}
    assert "content_warning" in by_name["a.pdf"]
    assert by_name["a.pdf"]["content_warning"]["evidence"] == "FLAGGED: test evidence"
    assert by_name["a.pdf"]["content_warning"]["pages_checked"] == 5
    assert "content_warning" not in by_name["b.pdf"]
    assert "content_warning" not in by_name["c.pdf"], "ground_truth-proposed files are out of scope for this check"


def test_build_ledger_with_no_warnings_omits_the_key_everywhere():
    plan = [(FakeFile("a.pdf"), "raw")]
    ledger = intake_case.build_ledger("CASE_009", "src", plan, {})
    assert "content_warning" not in ledger["files"][0]


def test_scan_for_answer_key_content_uses_capped_page_count(monkeypatch, tmp_path):
    """Regression: the scan must render only the first N pages, not the
    whole document -- verifies the max_pages plumbing into
    ocr_extract.split_to_page_images actually gets used, not silently
    ignored."""
    from unittest import mock

    fake_pages = [tmp_path / f"page_{i:03d}.png" for i in range(1, 4)]
    for p in fake_pages:
        p.write_bytes(b"fake png bytes")

    captured = {}

    def fake_split(doc_path, out_dir, max_pages=None):
        captured["max_pages"] = max_pages
        return fake_pages

    def fake_run(cmd, **kw):
        r = mock.Mock()
        r.returncode = 0
        r.stdout = "CLEAR"
        r.stderr = ""
        return r

    monkeypatch.setattr(intake_case, "split_to_page_images", fake_split)
    monkeypatch.setattr(intake_case.subprocess, "run", fake_run)

    result = intake_case.scan_for_answer_key_content(tmp_path / "doc.pdf", "CASE_009", 1, n_pages=3)

    assert captured["max_pages"] == 3
    assert result["flagged"] is False
    assert result["pages_checked"] == 3
