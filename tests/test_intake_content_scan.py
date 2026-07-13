"""intake_case.py's content pre-check (known-gaps.md item 2, fixing intake's
content-blind classification -- the CASE_002 incident: DOC_002/DOC_003
looked like plain claim documents by filename but were actually completed
third-party loss-adjustment reports with stated payout figures).

_parse_content_scan_verdict is pure and tested directly (no real provider
backend or page images needed). build_ledger's wiring is tested with fake plan
entries -- no real PDF needed there either.
"""
import pytest

import intake_case
from llm_providers import ProviderConfig, ProviderConfigError, ProviderResult


class FakeScanProvider:
    provider_name = "openai-api"
    model_name = "gpt-test"

    def __init__(self, response):
        self.response = response
        self.prompts = []

    def scan_intake_content(self, prompt, prompt_version):
        self.prompts.append((prompt, prompt_version))
        return ProviderResult(self.provider_name, self.model_name, prompt_version, self.response)


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
    fake_pages = [tmp_path / f"page_{i:03d}.png" for i in range(1, 4)]
    for p in fake_pages:
        p.write_bytes(b"fake png bytes")

    captured = {}

    def fake_split(doc_path, out_dir, max_pages=None):
        captured["max_pages"] = max_pages
        return fake_pages

    monkeypatch.setattr(intake_case, "split_to_page_images", fake_split)
    provider = FakeScanProvider("CLEAR")

    result = intake_case.scan_for_answer_key_content(tmp_path / "doc.pdf", "CASE_009", 1, n_pages=3, provider=provider)

    assert captured["max_pages"] == 3
    assert result["flagged"] is False
    assert result["pages_checked"] == 3
    assert len(provider.prompts) == 1
    assert provider.prompts[0][1] == intake_case.CONTENT_SCAN_PROMPT_VERSION


def test_scan_for_answer_key_content_records_provider_metadata_in_flagged_evidence(monkeypatch, tmp_path):
    fake_pages = [tmp_path / "page_001.png"]
    fake_pages[0].write_bytes(b"fake png bytes")

    monkeypatch.setattr(intake_case, "split_to_page_images", lambda doc_path, out_dir, max_pages=None: fake_pages)
    provider = FakeScanProvider("FLAGGED: reads 보험금사정서")

    result = intake_case.scan_for_answer_key_content(tmp_path / "doc.pdf", "CASE_009", 1, provider=provider)

    assert result["flagged"] is True
    assert "FLAGGED: reads 보험금사정서" in result["evidence"]
    assert "provider=openai-api" in result["evidence"]
    assert "model=gpt-test" in result["evidence"]
    assert result["provider_metadata"]["provider_name"] == "openai-api"


def test_scan_for_answer_key_content_unparseable_still_flags_with_metadata(monkeypatch, tmp_path):
    fake_pages = [tmp_path / "page_001.png"]
    fake_pages[0].write_bytes(b"fake png bytes")

    monkeypatch.setattr(intake_case, "split_to_page_images", lambda doc_path, out_dir, max_pages=None: fake_pages)
    provider = FakeScanProvider("garbled")

    result = intake_case.scan_for_answer_key_content(tmp_path / "doc.pdf", "CASE_009", 1, provider=provider)

    assert result["flagged"] is True
    assert "garbled" in result["evidence"]
    assert "provider=openai-api" in result["evidence"]


def test_build_scan_provider_uses_scan_specific_environment(monkeypatch):
    created = []

    class BuiltProvider:
        provider_name = "fixture"
        model_name = "scan-model"

    def fake_build_provider(config, **kwargs):
        created.append(config)
        return BuiltProvider()

    monkeypatch.setattr(intake_case, "build_provider", fake_build_provider)

    provider = intake_case.build_scan_provider(
        env={"HARNESS_INTAKE_SCAN_PROVIDER": "fixture", "HARNESS_INTAKE_SCAN_MODEL": "scan-model"}
    )

    assert provider.provider_name == "fixture"
    assert created == [ProviderConfig("fixture", "scan-model")]


def test_build_scan_provider_missing_openai_credentials_fail_clearly():
    with pytest.raises(ProviderConfigError) as excinfo:
        intake_case.build_scan_provider(scan_provider_name="openai-api", env={})

    assert "OPENAI_API_KEY" in str(excinfo.value)
