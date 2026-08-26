"""`content_warning`'s ledger handling, after the scan that wrote it was removed.

D2's vision content pre-check was removed on 2026-08-20 at the PoC owner's
direction, and with it the twelve tests that drove the scan, its provider
construction, and its verdict parsing. `build_ledger` still ACCEPTS a warning
mapping, because ledgers written while the scan ran carry `content_warning`
and the schema still declares it -- dropping the handling would fail those
files retroactively and hide a warning a reviewer was once shown.

The removal itself is asserted in `test_poc_guardrail_scope.py`; the human
approval gate the scan fed is asserted in `test_intake_case.py`.
"""
import intake_case


class FakeFile:
    def __init__(self, name):
        self.name = name


def test_build_ledger_only_flagged_files_get_content_warning():
    plan = [
        (FakeFile("a.pdf"), "raw"),
        (FakeFile("b.pdf"), "raw"),
        (FakeFile("c.pdf"), "ground_truth"),
    ]
    warnings = {"a.pdf": {"flagged": True, "evidence": "FLAGGED: test evidence",
                          "pages_checked": 5}}

    ledger = intake_case.build_ledger("CASE_009", "src", plan, {}, warnings)

    by_name = {f["file_name"]: f for f in ledger["files"]}
    assert "content_warning" in by_name["a.pdf"]
    assert by_name["a.pdf"]["content_warning"]["evidence"] == "FLAGGED: test evidence"
    assert by_name["a.pdf"]["content_warning"]["pages_checked"] == 5
    assert "content_warning" not in by_name["b.pdf"]
    assert "content_warning" not in by_name["c.pdf"], \
        "ground_truth-proposed files are out of scope for this check"


def test_build_ledger_with_no_warnings_omits_the_key_everywhere():
    """The live path now: nothing scans, so nothing is annotated."""
    plan = [(FakeFile("a.pdf"), "raw")]
    ledger = intake_case.build_ledger("CASE_009", "src", plan, {})
    assert "content_warning" not in ledger["files"][0]
