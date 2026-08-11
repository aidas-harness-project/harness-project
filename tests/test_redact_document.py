import json

import pytest

import redact_document as rd
from redaction import LlmRedactor, RedactionLeakError
from llm_providers import FixtureProvider


def _fixture_redactor(pii_items_json: str) -> LlmRedactor:
    provider = FixtureProvider(model_name="fixture-model", responses={"redact_text": pii_items_json})
    return LlmRedactor(provider)


OCR_RESULT = {"cross_validation_status": "agreed", "pages": [{"page": 1}, {"page": 2}]}


def _install_dao_stubs(monkeypatch, page_text, captured=None, calls=None):
    """Stub the DAO surface redact_document uses.

    The per-page read is now an in-process call (T4a: a subprocess per page
    multiplied ~0.38s of interpreter startup by page count), so it is stubbed
    separately from the subprocess writes. The capability assertion is kept on
    BOTH paths -- it is the check that stops a regression from reading
    pre-redaction text without checkpoint 2's token, and moving the call
    in-process must not be allowed to quietly drop it.
    """
    def fake_read_contract(case_id, filename):
        if calls is not None:
            calls.append(("read-contract", filename))
        return dict(OCR_RESULT) if filename.startswith("ocr_result") else {}

    def fake_read_page_text(case_id, doc_id, page, *, caller_stage, capability):
        if calls is not None:
            calls.append(("read-page-text", page))
        assert capability, "read_page_text_data must receive checkpoint 2's capability"
        assert caller_stage == "document-pipeline", (
            "the caller-stage gate must still be asserted in-process")
        return page_text  # page-invariant so one canned fixture fits both pages

    def fake_dao(*args, capability=None):
        if calls is not None:
            calls.append(args)
        assert capability is None, f"{args[0]} must not be handed the page-text capability"
        if captured is not None and args[0] == "write-redacted-text":
            captured["redacted"] = rd.Path(args[args.index("--text-file") + 1]).read_text(encoding="utf-8")
        if captured is not None and args[0] == "write-contract":
            captured["contract"] = json.loads(rd.Path(args[args.index("--data-file") + 1]).read_text(encoding="utf-8"))
        return "OK"

    monkeypatch.setattr(rd.dao, "read_contract_data", fake_read_contract)
    monkeypatch.setattr(rd.dao, "read_page_text_data", fake_read_page_text)
    monkeypatch.setattr(rd, "_dao", fake_dao)
    return fake_dao


def test_clean_redaction_writes_via_dao_and_flags_no_review(monkeypatch, tmp_path):
    calls, captured = [], {}
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절", captured, calls)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]}))

    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)

    assert [c[0] for c in calls].count("read-page-text") == 2
    assert captured["redacted"].startswith("<<<PAGE page=1>>>")
    assert "[PERSON_NAME]" in captured["redacted"]
    assert "홍길동" not in captured["redacted"]  # PII gone
    assert "진단 골절" in captured["redacted"]   # non-PII preserved by construction
    assert captured["contract"]["review_required"] is False
    assert captured["contract"]["method"] == "llm_span_redaction"
    assert captured["contract"]["items_redacted"] == 2  # one per page
    assert result["status"] == "success"


def test_residual_structured_pii_hard_fails_and_writes_nothing(monkeypatch, tmp_path):
    calls = []
    # Page has a phone number; the model lists only the name -> phone survives.
    _install_dao_stubs(monkeypatch, "환자 홍길동 010-1234-5678", calls=calls)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]}))

    with pytest.raises(RedactionLeakError) as exc:
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)
    assert "phone" in str(exc.value).lower()
    # nothing was written
    assert "write-redacted-text" not in [c[0] for c in calls]
    assert "write-contract" not in [c[0] for c in calls]


def test_unmatched_span_hard_fails(monkeypatch, tmp_path):
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    # Model names a person not present verbatim in the source.
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "김철수", "category": "person_name"}]}))

    with pytest.raises(RedactionLeakError) as exc:
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)
    assert "verbatim" in str(exc.value).lower()


def test_over_redaction_ambiguous_span_writes_with_review(monkeypatch, tmp_path):
    captured = {}
    _install_dao_stubs(monkeypatch, "이 사람은 이번 사고를 겪었다", captured)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    # A 1-char "name" -> over-redaction guard leaves it, flags review (no leak).
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "이", "category": "person_name"}]}))

    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)

    assert captured["contract"]["review_required"] is True
    assert any("not redacted" in w for w in captured["contract"]["warnings"])
    assert result["review_required"] is True


# ------------------------------------------------- T4b: per-page resume cache --

def test_second_run_serves_from_cache_and_makes_no_provider_call(monkeypatch, tmp_path):
    calls, captured = [], {}
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절", captured, calls)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})

    first = _fixture_redactor(items)
    r1 = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", first)
    assert r1["cache_hits"] == 0

    # A provider that would RAISE if consulted -- the only honest way to show
    # the second run made no call.
    class _Exploding:
        method = "llm_span_redaction"
        label = first.label

        def redact_page(self, text):
            raise AssertionError("cache miss: the provider was called on a warm run")

    r2 = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", _Exploding())
    assert r2["cache_hits"] == 2
    assert r2["items_redacted"] == r1["items_redacted"]
    assert r2["review_required"] == r1["review_required"]


def test_cached_redaction_is_byte_identical_to_the_fresh_one(monkeypatch, tmp_path):
    """A cache that returns different bytes is worse than no cache."""
    cold, warm = {}, {}
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절", cold)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})
    redactor = _fixture_redactor(items)
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)

    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절", warm)
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _fixture_redactor(items))
    assert warm["redacted"] == cold["redacted"]


def test_a_prompt_version_change_invalidates_the_cache(monkeypatch, tmp_path):
    """The privacy-critical case. A prompt revision usually means the previous
    prompt MISSED something, so serving its output afterwards is not a stale
    performance number -- it is un-redacted PII reaching the processed layer."""
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _fixture_redactor(items))

    monkeypatch.setattr(rd, "PROMPT_VERSION", "pii_redaction_v999")
    called = {"n": 0}
    base = _fixture_redactor(items)

    class _Counting:
        method = "llm_span_redaction"
        label = base.label

        def redact_page(self, text):
            called["n"] += 1
            return base.redact_page(text)

    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                                _Counting())
    assert result["cache_hits"] == 0
    assert called["n"] == 2  # both pages genuinely re-redacted


def test_a_different_model_does_not_reuse_another_models_entry(monkeypatch, tmp_path):
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _fixture_redactor(items))

    other = LlmRedactor(FixtureProvider(model_name="a-different-model",
                                        responses={"redact_text": items}))
    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", other)
    assert result["cache_hits"] == 0


def test_changed_page_text_invalidates_the_cache(monkeypatch, tmp_path):
    """Checkpoint 1 output can change (a P8 disagreement resolved, a page
    re-transcribed). The old redaction was computed against text that no
    longer exists."""
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _fixture_redactor(items))

    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절 (재전사됨)")
    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                                _fixture_redactor(items))
    assert result["cache_hits"] == 0


def test_a_leaking_page_is_never_cached(monkeypatch, tmp_path):
    """A blocked document must leave nothing reusable behind -- otherwise a
    rerun would serve the very output the leak check refused."""
    _install_dao_stubs(monkeypatch, "환자 홍길동 010-1234-5678")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})
    with pytest.raises(RedactionLeakError):
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                           _fixture_redactor(items))
    cache_dir = rd._resume_cache_dir("CASE_009", "DOC_001")
    assert not list(cache_dir.glob("page_*.json"))


def test_a_corrupt_cache_entry_is_a_miss_not_a_crash(monkeypatch, tmp_path):
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _fixture_redactor(items))
    cache_dir = rd._resume_cache_dir("CASE_009", "DOC_001")
    (cache_dir / "page_001.json").write_text("{ truncated", encoding="utf-8")

    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                                _fixture_redactor(items))
    assert result["cache_hits"] == 1  # page 2 still hit, page 1 re-run
    assert result["status"] == "success"


def test_no_resume_ignores_an_existing_cache(monkeypatch, tmp_path):
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    items = json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]})
    rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                       _fixture_redactor(items))
    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1",
                                _fixture_redactor(items), resume=False)
    assert result["cache_hits"] == 0


# ------------------------------------------------- default provider is usable --

def test_default_redaction_provider_resolves_to_a_launchable_command():
    """The default must be a provider that can actually start.

    codex-cli was the default until 2026-08-11, and on Windows it cannot be
    launched at all: the npm shim installs as `codex.CMD`, which `shutil.which`
    resolves happily but `subprocess.run(["codex"])` rejects with WinError 2 --
    a batch file needs a shell, not execve. An operator who passed no
    --provider got a FileNotFoundError instead of a redaction.

    Asserting the constant's spelling would only restate the code. This builds
    the provider the default actually selects and checks the command it would
    launch is a real executable file, which is the property that broke.
    """
    import shutil
    from pathlib import Path as _Path

    from llm_providers import ProviderConfig, build_provider

    provider = build_provider(
        ProviderConfig(provider_name=rd.DEFAULT_REDACTION_PROVIDER))
    command = getattr(provider, "command", None)
    assert command, "the default provider must expose the command it launches"

    resolved = shutil.which(command) or (command if _Path(command).exists() else None)
    assert resolved, (
        f"default provider {rd.DEFAULT_REDACTION_PROVIDER!r} resolves to "
        f"{command!r}, which is not on PATH and is not an existing file")
    assert _Path(resolved).suffix.lower() not in (".cmd", ".bat", ".ps1"), (
        f"{resolved!r} is a shell script shim; subprocess.run(shell=False) "
        "cannot launch it on Windows, and the cmd.exe layer truncates "
        "multi-line prompts at the first blank line")
