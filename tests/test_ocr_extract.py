"""ocr_extract.py -- compare() verdict parsing, scratch_dir placement, and
provider-agnostic P8 wiring.

Provider calls are faked throughout -- these tests never shell out to a
real `claude` binary and never call an external API.
"""
import pathlib
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import llm_providers
import ocr_extract as oe
from llm_providers import ProviderConfig, ProviderConfigError, ProviderResult


class FakeComparator:
    provider_name = "fixture"
    model_name = "fixture-comparator"

    def __init__(self, verdict):
        self.verdict = verdict
        self.prompts = []

    def compare_text(self, prompt, prompt_version):
        self.prompts.append((prompt, prompt_version))
        return ProviderResult(self.provider_name, self.model_name, prompt_version, self.verdict)


class FakeReader:
    def __init__(self, provider_name, model_name, readings):
        self.provider_name = provider_name
        self.model_name = model_name
        self.readings = list(readings)
        self.calls = []

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str):
        self.calls.append((image_path, prompt, prompt_version))
        return ProviderResult(self.provider_name, self.model_name, prompt_version, self.readings.pop(0))


def test_transcribe_once_returns_text_and_provider_metadata(tmp_path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake image")
    reader = FakeReader("openai-api", "test-model", ["page text"])

    result = oe.transcribe_once(image_path, reader)

    assert result["text"] == "page text"
    assert result["metadata"]["provider_name"] == "openai-api"
    assert result["metadata"]["model_name"] == "test-model"
    assert result["metadata"]["prompt_version"] == oe.OCR_PROMPT_VERSION


@pytest.mark.parametrize("verdict,expected", [
    ("AGREE: same facts", "agreed"),
    ("DISAGREE: date mismatch", "disagreed"),
    ("The two transcriptions AGREE on all major facts.", "agreed"),
    ("I have to say these transcriptions DISAGREE on the diagnosis code.", "disagreed"),
    ("completely garbled output with neither token", "disagreed"),
])
def test_compare_word_boundary_parsing(verdict, expected):
    result = oe.compare("text A", "text B", FakeComparator(verdict))

    assert result["agreement"] == expected


def test_compare_identical_texts_shortcuts_without_calling_comparator():
    """Byte-identical reads trivially agree -- compare() must short-circuit and
    NOT spend a comparator provider call (parity with shared/main). This does
    not relax P8: only the trivially-agreed case is shortcut; any difference
    still goes through the comparator (covered by the other compare() tests)."""
    comparator = FakeComparator("AGREE: identical text")

    result = oe.compare("same text", "same text", comparator)

    assert result["agreement"] == "agreed"
    assert result["disagreement_details"] == []
    assert len(comparator.prompts) == 0  # comparator was never invoked
    assert result["metadata"]["comparator_called"] is False


def test_compare_shortcut_ignores_surrounding_whitespace():
    """The shortcut compares after .strip(), so reads that differ only in
    leading/trailing whitespace still take the no-call agreed path."""
    comparator = FakeComparator("AGREE")

    result = oe.compare("  same text\n", "same text", comparator)

    assert result["agreement"] == "agreed"
    assert len(comparator.prompts) == 0


def test_compare_disagree_substring_inside_word_does_not_false_trigger():
    """'DISAGREE' contains 'AGREE' as a substring but not on a word
    boundary -- must not be misread as an AGREE verdict."""
    result = oe.compare("a", "b", FakeComparator("DISAGREE"))

    assert result["agreement"] == "disagreed"


def test_unparseable_verdict_records_the_raw_text_for_audit():
    result = oe.compare("a", "b", FakeComparator("???"))

    assert result["agreement"] == "disagreed"
    assert "???" in result["disagreement_details"][0]


def test_compare_records_comparator_metadata():
    result = oe.compare("a", "b", FakeComparator("AGREE: same"))

    assert result["metadata"]["provider_name"] == "fixture"
    assert result["metadata"]["model_name"] == "fixture-comparator"
    assert result["metadata"]["prompt_version"] == oe.COMPARE_PROMPT_VERSION


def test_scratch_dir_is_project_local_not_system_tmp():
    with oe.scratch_dir("CASE_009", "DOC_001") as d:
        assert str(oe.ROOT) in str(d)
        assert not str(d).startswith(tempfile.gettempdir())
        assert d.exists()
    assert not d.exists(), "scratch dir must be cleaned up on exit"


def test_scratch_dir_cleans_up_even_on_exception():
    d_ref = {}
    with pytest.raises(RuntimeError):
        with oe.scratch_dir("CASE_009", "DOC_001") as d:
            d_ref["d"] = d
            raise RuntimeError("boom")
    assert not d_ref["d"].exists()


def test_compare_prompt_asks_about_one_sided_extraneous_content():
    """known-gaps.md item 11: compare()'s original prompt only checked for
    conflicting facts, so a fabricated appendix present in only one reading
    (no conflicting fact anywhere) slipped through as 'agreed' on a real
    document. The prompt must explicitly ask about content one reading has
    that the other lacks, not just fact conflicts -- lock that in so a
    future edit can't silently drop it."""
    prompt = oe.COMPARE_PROMPT_TEMPLATE
    assert "the other" in prompt and "lacks" in prompt
    assert "hallucinated" in prompt.lower()


def test_scratch_dir_distinct_per_process_id(monkeypatch):
    """PID-tagged so a retry racing a stale process can't collide on one path."""
    monkeypatch.setattr(oe.os, "getpid", lambda: 111)
    with oe.scratch_dir("CASE_009", "DOC_001") as d1:
        path1 = d1
    monkeypatch.setattr(oe.os, "getpid", lambda: 222)
    with oe.scratch_dir("CASE_009", "DOC_001") as d2:
        path2 = d2
    assert path1 != path2


def test_split_to_page_images_falls_back_to_pdftoppm(monkeypatch, tmp_path):
    def missing_fitz(*args, **kwargs):
        raise ImportError("fitz missing")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        Path(f"{cmd[-1]}-1.png").write_bytes(b"fake png")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(oe, "_split_to_page_images_fitz", missing_fitz)
    monkeypatch.setattr(oe, "_find_pdftoppm", lambda: "pdftoppm")
    monkeypatch.setattr(oe.subprocess, "run", fake_run)

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    out_dir = tmp_path / "pages"
    out_dir.mkdir()

    result = oe.split_to_page_images(pdf_path, out_dir, max_pages=1)

    assert result == [out_dir / "page_001.png"]
    assert result[0].exists()
    assert captured["cmd"] == ["pdftoppm", "-png", "-r", "200", "-f", "1", "-l", "1", str(pdf_path), str(out_dir / "page")]
    assert captured["kwargs"]["timeout"] == 120


def test_build_ocr_providers_supports_same_openai_provider_twice(monkeypatch):
    created = []

    class BuiltProvider:
        def __init__(self, config):
            self.provider_name = config.provider_name
            self.model_name = config.model_name or "fake-model"

    def fake_build_provider(config, **kwargs):
        created.append(config)
        return BuiltProvider(config)

    monkeypatch.setattr(oe, "build_provider", fake_build_provider)

    providers = oe.build_ocr_providers(
        reader_a_name="openai-api",
        reader_b_name="openai-api",
        comparator_name="openai-api",
        env={"OPENAI_API_KEY": "secret"},
    )

    assert [c.provider_name for c in created] == ["openai-api", "openai-api", "openai-api"]
    assert providers["reader_a"] is not providers["reader_b"]
    assert providers["reader_a"].provider_name == "openai-api"
    assert providers["reader_b"].provider_name == "openai-api"


def test_build_ocr_providers_missing_openai_credentials_fail_clearly():
    with pytest.raises(ProviderConfigError) as excinfo:
        oe.build_ocr_providers(
            reader_a_name="openai-api",
            reader_b_name="openai-api",
            comparator_name="openai-api",
            env={},
        )

    assert "openai-api" in str(excinfo.value)
    assert "OPENAI_API_KEY" in str(excinfo.value)


def test_run_ocr_with_api_style_providers_does_not_require_claude_cli(monkeypatch, tmp_path):
    def fail_if_claude_cli_called(*args, **kwargs):
        raise AssertionError("claude CLI must not be used when providers are supplied")

    monkeypatch.setattr(llm_providers.subprocess, "run", fail_if_claude_cli_called)

    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake image")
    reader_a = FakeReader("openai-api", "same-model", ["reader A text"])
    reader_b = FakeReader("openai-api", "same-model", ["reader B text"])
    comparator = FakeComparator("AGREE: same material facts")

    result = oe.run_ocr("CASE_009", "DOC_001", image_path, reader_a=reader_a, reader_b=reader_b, comparator=comparator)

    assert len(reader_a.calls) == 1
    assert len(reader_b.calls) == 1
    assert result["providers"]["reader_a"] == {"provider_name": "openai-api", "model_name": "same-model"}
    assert result["providers"]["reader_b"] == {"provider_name": "openai-api", "model_name": "same-model"}
    assert result["pages"][0]["agreement"] == "agreed"
    assert result["pages"][0]["reading_a"] == "reader A text"
    assert result["pages"][0]["reading_b"] == "reader B text"
    assert result["pages"][0]["provider_metadata"]["reader_a"]["provider_name"] == "openai-api"


def test_build_ocr_providers_env_defaults_reader_b_and_comparator_to_reader_a(monkeypatch):
    created: list[ProviderConfig] = []

    def fake_build_provider(config, **kwargs):
        created.append(config)
        return FakeReader(config.provider_name, config.model_name or "model", ["x"])

    monkeypatch.setattr(oe, "build_provider", fake_build_provider)

    oe.build_ocr_providers(env={"HARNESS_OCR_READER_A_PROVIDER": "fixture", "HARNESS_OCR_READER_A_MODEL": "fixture-v1"})

    assert [c.provider_name for c in created] == ["fixture", "fixture", "fixture"]
    assert [c.model_name for c in created] == ["fixture-v1", "fixture-v1", "fixture-v1"]


def test_build_ocr_providers_can_fall_back_to_common_llm_environment(monkeypatch):
    created: list[ProviderConfig] = []

    def fake_build_provider(config, **kwargs):
        created.append(config)
        return FakeReader(config.provider_name, config.model_name or "model", ["x"])

    monkeypatch.setattr(oe, "build_provider", fake_build_provider)

    oe.build_ocr_providers(env={"HARNESS_LLM_PROVIDER": "fixture", "HARNESS_LLM_MODEL": "common-model"})

    assert [c.provider_name for c in created] == ["fixture", "fixture", "fixture"]
    assert [c.model_name for c in created] == ["common-model", "common-model", "common-model"]


def _patch_two_page_split(monkeypatch, tmp_path):
    """Make run_ocr see a deterministic 2-page document without a real PDF."""
    def fake_split(doc_path, out_dir, max_pages=None):
        imgs = []
        for n in (1, 2):
            p = out_dir / f"page_{n:03d}.png"
            p.write_bytes(b"img")
            imgs.append(p)
        return imgs
    monkeypatch.setattr(oe, "split_to_page_images", fake_split)
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    return doc


def test_run_ocr_caches_pages_and_resume_skips_reader_calls(monkeypatch, tmp_path):
    """An interrupted multi-page run must resume without re-transcribing pages
    it already finished -- that's the whole point of the resume cache for a
    75-page document that otherwise loses everything on any interruption."""
    monkeypatch.setattr(oe, "SCRATCH_ROOT", tmp_path / "_ocr_scratch")
    doc = _patch_two_page_split(monkeypatch, tmp_path)

    # First run: only page 1 succeeds, page 2 raises mid-way (simulated crash).
    reader_a = FakeReader("fixture", "m", ["A1", "A2"])
    reader_b = FakeReader("fixture", "m", ["B1"])  # runs out on page 2 -> IndexError
    comparator = FakeComparator("AGREE")
    with pytest.raises(IndexError):
        oe.run_ocr("CASE_009", "DOC_001", doc, reader_a=reader_a, reader_b=reader_b, comparator=comparator)

    cache = oe._resume_cache_dir("CASE_009", "DOC_001")
    assert (cache / "page_001.json").exists()
    assert not (cache / "page_002.json").exists()

    # Second run: fresh readers whose page-1 output would DIFFER if called.
    # Page 1 must come from cache (readers NOT called for it); only page 2 runs.
    reader_a2 = FakeReader("fixture", "m", ["A2"])
    reader_b2 = FakeReader("fixture", "m", ["B2"])
    result = oe.run_ocr("CASE_009", "DOC_001", doc, reader_a=reader_a2, reader_b=reader_b2, comparator=FakeComparator("AGREE"))

    assert [p["page"] for p in result["pages"]] == [1, 2]
    assert result["pages"][0]["reading_a"] == "A1"  # from cache, not "A2"
    assert len(reader_a2.calls) == 1  # only page 2 re-read
    # Cache cleared after full completion.
    assert not cache.exists()


def test_run_ocr_resume_false_ignores_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(oe, "SCRATCH_ROOT", tmp_path / "_ocr_scratch")
    doc = _patch_two_page_split(monkeypatch, tmp_path)

    # Seed a cache entry for page 1 that resume=False must ignore.
    cache = oe._resume_cache_dir("CASE_009", "DOC_001")
    oe._save_cached_page(cache, 1, {"page": 1, "reading_a": "STALE", "reading_b": "STALE",
                                    "agreement": "agreed", "disagreement_details": [], "provider_metadata": {}})

    reader_a = FakeReader("fixture", "m", ["A1", "A2"])
    reader_b = FakeReader("fixture", "m", ["B1", "B2"])
    result = oe.run_ocr("CASE_009", "DOC_001", doc, reader_a=reader_a, reader_b=reader_b,
                        comparator=FakeComparator("AGREE"), resume=False)

    assert result["pages"][0]["reading_a"] == "A1"  # freshly read, not "STALE"
    assert len(reader_a.calls) == 2  # both pages read
