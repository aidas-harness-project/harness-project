from pathlib import Path
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

import llm_providers as providers


def test_parse_provider_config_defaults_to_claude_cli():
    config = providers.parse_provider_config(env={})

    assert config.provider_name == "claude-cli"
    assert config.model_name is None


def test_parse_provider_config_prefers_cli_args_over_environment():
    args = SimpleNamespace(provider="openai-api", model="cli-model")

    config = providers.parse_provider_config(
        args,
        env={"HARNESS_LLM_PROVIDER": "anthropic-api", "HARNESS_LLM_MODEL": "env-model"},
    )

    assert config.provider_name == "openai-api"
    assert config.model_name == "cli-model"


def test_parse_provider_config_reads_environment_when_args_omitted():
    config = providers.parse_provider_config(
        env={"HARNESS_LLM_PROVIDER": "fixture", "HARNESS_LLM_MODEL": "fixture-v1"},
    )

    assert config.provider_name == "fixture"
    assert config.model_name == "fixture-v1"


def test_build_provider_selects_claude_cli_by_default(tmp_path):
    provider = providers.build_provider(env={}, root=tmp_path)

    assert isinstance(provider, providers.ClaudeCliProvider)
    assert provider.provider_name == "claude-cli"
    assert provider.model_name == "claude-cli"
    assert provider.root == tmp_path


def test_claude_cli_provider_can_read_command_from_environment(tmp_path):
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="claude-cli"),
        env={"HARNESS_CLAUDE_COMMAND": "C:/tools/claude.exe"},
        root=tmp_path,
    )

    assert provider.command == "C:/tools/claude.exe"


def test_codex_cli_provider_can_read_command_from_environment(tmp_path):
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="codex-cli"),
        env={"HARNESS_CODEX_COMMAND": "C:/tools/codex.exe"},
        root=tmp_path,
    )

    assert provider.command == "C:/tools/codex.exe"


@pytest.mark.parametrize(("provider_name", "key_name"), [
    ("anthropic-api", "ANTHROPIC_API_KEY"),
    ("openai-api", "OPENAI_API_KEY"),
])
def test_api_provider_missing_credentials_fail_clearly(provider_name, key_name):
    config = providers.ProviderConfig(provider_name=provider_name)

    with pytest.raises(providers.ProviderConfigError) as excinfo:
        providers.build_provider(config, env={})

    assert provider_name in str(excinfo.value)
    assert key_name in str(excinfo.value)


@pytest.mark.parametrize(("provider_name", "provider_type", "key_name"), [
    ("anthropic-api", providers.AnthropicApiProvider, "ANTHROPIC_API_KEY"),
    ("openai-api", providers.OpenAIApiProvider, "OPENAI_API_KEY"),
])
def test_api_provider_selection_with_credentials(provider_name, provider_type, key_name):
    config = providers.ProviderConfig(provider_name=provider_name, model_name="test-model")

    provider = providers.build_provider(config, env={key_name: "secret"})

    assert isinstance(provider, provider_type)
    assert provider.provider_name == provider_name
    assert provider.model_name == "test-model"


def test_openai_provider_can_read_model_from_provider_specific_environment():
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api"),
        env={"OPENAI_API_KEY": "secret", "HARNESS_OPENAI_MODEL": "configured-model"},
    )

    assert provider.model_name == "configured-model"


def test_anthropic_provider_stub_execution_error_is_clear():
    config = providers.ProviderConfig(provider_name="anthropic-api", model_name="claude-x")
    provider = providers.build_provider(config, env={"ANTHROPIC_API_KEY": "secret"})

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.classify_document("prompt", "classification_v0.1")

    assert "anthropic-api" in str(excinfo.value)
    assert "not implemented" in str(excinfo.value)


def test_api_stub_without_model_fails_at_build_not_call():
    # 2-5: a stub API provider must not fabricate a "<provider>-model" string
    # that 404s on the first page. Selecting it with no model resolvable fails
    # at build (ProviderConfigError), pointing at how to fix it.
    config = providers.ProviderConfig(provider_name="anthropic-api")
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        providers.build_provider(config, env={"ANTHROPIC_API_KEY": "secret"})
    assert "model" in str(excinfo.value).lower()


def test_fixture_provider_returns_common_result_shape():
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="fixture", model_name="fixture-model"),
        env={},
        fixture_responses={"classify_document": '{"predicted_document_type": "other"}'},
    )

    result = provider.classify_document("prompt", "classification_v0.1")

    assert result.provider_name == "fixture"
    assert result.model_name == "fixture-model"
    assert result.prompt_version == "classification_v0.1"
    assert result.text == '{"predicted_document_type": "other"}'
    assert result.metadata() == {
        "provider_name": "fixture",
        "model_name": "fixture-model",
        "prompt_version": "classification_v0.1",
        "raw_metadata": {"fixture_key": "classify_document"},
        "finish_reason": None,
    }


def test_claude_cli_provider_preserves_current_transcription_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        result = mock.Mock()
        result.returncode = 0
        result.stdout = "transcribed text"
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    img = tmp_path / "pages" / "page.png"
    result = provider.transcribe_image(img, "transcribe prompt", "ocr_extraction_v0.1")

    # The transcription prompt must stay NEUTRAL -- no defensive "role framing"
    # preamble (a prior version prepended "this is a SANCTIONED step, do not
    # refuse..." which the nested claude read as a prompt-injection signal and
    # refused). The image is referenced as an explicit Read instruction, not a
    # trailing "Image: {path}" label (the label form is read as metadata about a
    # never-arriving attachment and the Read tool is never invoked). This
    # assertion guards both against framing creeping back in.
    # The prompt now travels on stdin (Windows CreateProcess 32,767-char argv
    # limit), so the framing assertion moves to the stdin payload -- it still
    # guards exactly the same thing: no anti-refusal preamble, and the image
    # referenced as an explicit Read instruction.
    assert captured["cmd"] == [
        "claude",
        "-p",
        "--safe-mode",
        "--allowedTools",
        "Read",
    ]
    assert captured["kwargs"]["input"] == (
        f"Read the image file at {img} and then: transcribe prompt"
    )
    # H1: the Read-enabled child is confined to the image's own directory, not
    # the repo root -- an injected "also read data/ground_truth/..." can't reach.
    assert captured["kwargs"]["cwd"] == str(img.resolve().parent)
    assert captured["kwargs"]["timeout"] == 180
    assert result.text == "transcribed text"
    assert result.metadata()["provider_name"] == "claude-cli"


def test_claude_cli_structured_image_uses_native_json_schema(monkeypatch, tmp_path):
    captured = {}
    structured = {
        "boundaries": [{"page": 1}],
        "continuations": [2],
        "needs_full_page": [],
    }

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        result = mock.Mock()
        result.returncode = 0
        result.stdout = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-1",
            "structured_output": structured,
        })
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)
    image = tmp_path / "sheets" / "sheet.png"
    schema = {
        "type": "object",
        "properties": {"boundaries": {"type": "array"}},
        "required": ["boundaries"],
    }

    result = provider.analyze_image_structured(
        image, "analyze", "segment_v1", schema
    )

    assert "--output-format" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--output-format") + 1] == "json"
    assert json.loads(captured["cmd"][captured["cmd"].index("--json-schema") + 1]) == schema
    assert "--safe-mode" in captured["cmd"]
    assert captured["cmd"][-2:] == ["--allowedTools", "Read"]
    assert captured["kwargs"]["cwd"] == str(image.resolve().parent)
    assert result.structured_output == structured
    assert json.loads(result.text) == structured


def test_claude_cli_structured_image_fails_closed_without_structured_output(
    monkeypatch, tmp_path
):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = mock.Mock()
        result.returncode = 0
        result.stdout = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "explanatory prose",
        })
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.analyze_image_structured(
            tmp_path / "sheet.png",
            "analyze",
            "segment_v1",
            {"type": "object"},
        )

    # Caller owns the single P4 correction; the provider must not hide three
    # whole-model content retries behind one apparent attempt.
    assert len(calls) == 1
    assert "structured_output" in str(excinfo.value)


def test_claude_cli_structured_text_adapter_normalizes_native_envelope(monkeypatch, tmp_path):
    captured = {}
    schema = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    structured = {"ok": True}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        result = mock.Mock()
        result.returncode = 0
        result.stdout = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": structured,
        })
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    result = providers.ClaudeCliProvider(root=tmp_path).analyze_text_structured(
        "extract", "driver_extract_v1", schema
    )

    assert json.loads(captured["cmd"][captured["cmd"].index("--json-schema") + 1]) == schema
    assert result.structured_output == structured
    assert json.loads(result.text) == structured
    assert result.provider_name == "claude-cli"


def test_fixture_provider_supports_structured_text_driver_surface():
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="fixture", model_name="fixture-model"),
        env={}, fixture_responses={"analyze_text_structured": '{"ok": true}'},
    )

    result = provider.analyze_text_structured(
        "prompt", "driver_v0.1",
        {"type": "object", "required": ["ok"]},
    )

    assert result.structured_output == {"ok": True}
    assert result.text == '{"ok": true}'


def test_codex_cli_structured_text_adapter_uses_schema_file_and_cleans_up(
    monkeypatch, tmp_path
):
    captured = {}
    schema = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    structured = {"ok": True}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["schema_path"] = Path(cmd[cmd.index("--output-schema") + 1])
        captured["output_path"] = Path(cmd[cmd.index("--output-last-message") + 1])
        captured["schema"] = json.loads(captured["schema_path"].read_text(encoding="utf-8"))
        captured["output_path"].write_text(json.dumps(structured), encoding="utf-8")
        result = mock.Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    result = providers.CodexCliProvider(root=tmp_path).analyze_text_structured(
        "extract", "driver_extract_v1", schema
    )

    assert captured["schema"] == schema
    assert captured["cmd"].count("--output-schema") == 1
    assert result.structured_output == structured
    assert json.loads(result.text) == structured
    assert result.raw_metadata["structured_output_native"] is True
    assert not captured["schema_path"].exists()
    assert not captured["output_path"].exists()


def test_codex_cli_structured_text_adapter_fails_once_on_non_json(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
            "not json", encoding="utf-8"
        )
        result = mock.Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    with pytest.raises(providers.ProviderExecutionError, match="non-JSON"):
        providers.CodexCliProvider(root=tmp_path).analyze_text_structured(
            "extract", "driver_extract_v1", {"type": "object"}
        )
    assert len(calls) == 1


def test_codex_cli_structured_text_falls_back_for_dynamic_object_schema(monkeypatch, tmp_path):
    """Codex strict mode cannot express dynamic contract field names."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
            '{"fields": {}}', encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    schema = {
        "type": "object",
        "properties": {
            "fields": {"type": "object", "additionalProperties": {"type": "object"}},
        },
    }

    result = providers.CodexCliProvider(root=tmp_path).analyze_text_structured(
        "extract", "driver_extract_v1", schema
    )

    assert "--output-schema" not in captured["cmd"]
    assert result.structured_output == {"fields": {}}
    assert result.raw_metadata["structured_output_native"] is False


def test_claude_structured_text_keeps_dynamic_contract_schema(monkeypatch, tmp_path):
    """Claude may retain the same dynamic claim-fields contract natively."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        payload = {"structured_output": {"fields": {}}, "subtype": "success"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    schema = {
        "type": "object",
        "properties": {
            "fields": {"type": "object", "additionalProperties": {"type": "object"}},
        },
    }
    result = providers.ClaudeCliProvider(root=tmp_path).analyze_text_structured(
        "extract", "driver_extract_v1", schema
    )

    sent_schema = json.loads(captured["cmd"][captured["cmd"].index("--json-schema") + 1])
    assert sent_schema["properties"]["fields"]["additionalProperties"] == {"type": "object"}
    assert captured["kwargs"]["input"] == "extract"
    assert result.structured_output == {"fields": {}}

def test_claude_cli_provider_always_passes_safe_mode(monkeypatch, tmp_path):
    """Regression (CASE_022 real run): claude -p with cwd=ROOT auto-loads the
    project's CLAUDE.md/hooks, and a context-aware reader editorializes --
    both blind reads appended similar D2 meta-commentary to transcribed
    pages, which compare() waved through as agreement, contaminating the
    trusted processed layer. Every claude-cli call through this provider
    (transcribe/compare/classify/scan) must pass --safe-mode so the reader
    sees nothing but its own prompt."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = mock.Mock()
        result.returncode = 0
        result.stdout = "AGREE"
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    provider.transcribe_image(Path("page.png"), "transcribe prompt", "ocr_extraction_v0.1")
    provider.compare_text("compare prompt", "ocr_compare_v0.1")
    provider.classify_document("classify prompt", "classification_v0.1")
    provider.scan_intake_content("scan prompt", "intake_scan_v0.1", image_paths=[Path("p1.png")])

    assert len(calls) == 4
    for cmd in calls:
        assert "--safe-mode" in cmd, f"claude-cli call missing --safe-mode: {cmd}"


def test_claude_cli_provider_reports_missing_command(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path, command="missing-claude")

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert "claude-cli command not found: missing-claude" in str(excinfo.value)


def test_claude_cli_provider_never_leaves_child_waiting_on_stdin(monkeypatch, tmp_path):
    """The child must never block waiting for input it will not get.

    Originally enforced with stdin=DEVNULL: without it the child blocks ~3s
    ('no stdin data received in 3s') and intermittently exits non-zero on a
    live pipe -- the cause of CASE_003/DOC_008 dying mid-run. The prompt now
    travels on stdin, which satisfies the same guarantee more directly (the
    child reads the prompt, then sees EOF when subprocess closes the pipe).
    DEVNULL must be GONE, not merely unused: subprocess.run raises if both
    input= and stdin= are passed.
    """
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        result = mock.Mock()
        result.returncode = 0
        result.stdout = "ok"
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert "stdin" not in captured["kwargs"]
    assert captured["kwargs"]["input"] == "compare prompt"


def test_claude_cli_provider_retries_transient_failure_then_succeeds(monkeypatch, tmp_path):
    """A single non-zero exit must not kill the call -- a bounded retry
    absorbs a transient hiccup so a 75-page run survives one bad page."""
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(1)
        result = mock.Mock()
        if len(attempts) == 1:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "no stdin data received in 3s"
        else:
            result.returncode = 0
            result.stdout = "recovered text"
            result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    result = provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert len(attempts) == 2
    assert result.text == "recovered text"
    assert result.metadata()["raw_metadata"]["attempts"] == 2


def test_claude_cli_provider_raises_after_exhausting_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(1)
        result = mock.Mock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "persistent failure"
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert len(attempts) == providers._CLAUDE_CLI_MAX_ATTEMPTS
    assert "persistent failure" in str(excinfo.value)


def test_claude_cli_provider_does_not_retry_missing_command(monkeypatch, tmp_path):
    """FileNotFoundError is not transient -- it must raise immediately, not
    burn all retry attempts."""
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(1)
        raise FileNotFoundError("missing")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path, command="missing-claude")

    with pytest.raises(providers.ProviderExecutionError):
        provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert len(attempts) == 1


def test_codex_cli_provider_reads_output_last_message(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("transcribed text", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="noisy log", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.CodexCliProvider(model_name="gpt-test", root=tmp_path)

    result = provider.transcribe_image(Path("page.png"), "transcribe prompt", "ocr_extraction_v0.1")

    assert captured["cmd"][:7] == [
        "codex", "exec", "-", "--skip-git-repo-check",
        "--sandbox", "read-only", "--model",
    ]
    assert captured["cmd"][7:11] == ["gpt-test", "--image", "page.png", "--output-last-message"]
    output_path = Path(captured["cmd"][11])
    assert not output_path.exists()
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["timeout"] == 180
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["input"] == "transcribe prompt"
    assert result.text == "transcribed text"
    assert result.provider_name == "codex-cli"


def test_codex_cli_provider_passes_optional_api_key(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("ok", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="codex-cli"),
        env={"CODEX_API_KEY": "secret"},
        root=tmp_path,
    )

    provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert captured["env"]["CODEX_API_KEY"] == "secret"


def test_codex_cli_provider_reports_nonzero_exit_and_cleans_output(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["output_path"] = Path(cmd[cmd.index("--output-last-message") + 1])
        return SimpleNamespace(returncode=2, stdout="", stderr="authentication required")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.CodexCliProvider(root=tmp_path)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert "codex-cli call failed: authentication required" in str(excinfo.value)
    assert not captured["output_path"].exists()


def test_codex_cli_provider_reports_missing_command(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.CodexCliProvider(root=tmp_path, command="missing-codex")

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.classify_document("classify prompt", "classification_v0.1")

    assert "codex-cli command not found: missing-codex" in str(excinfo.value)


def test_openai_provider_posts_text_to_responses_api(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "resp_1", "status": "completed", "output_text": "AGREE: same"}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api", model_name="gpt-test"),
        env={"OPENAI_API_KEY": "secret"},
    )

    result = provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert captured["url"].endswith("/responses")
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"] == {"model": "gpt-test", "input": "compare prompt"}
    assert captured["timeout"] == 60
    assert result.text == "AGREE: same"
    assert result.raw_metadata["response_id"] == "resp_1"


def test_openai_provider_posts_base64_image_to_responses_api(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "resp_2", "status": "completed", "output_text": "page text"}'

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake png")
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api", model_name="gpt-test"),
        env={"OPENAI_API_KEY": "secret"},
    )

    result = provider.transcribe_image(image_path, "transcribe prompt", "ocr_extraction_v0.1")

    content = captured["payload"]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "transcribe prompt"}
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "high"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert captured["timeout"] == 180
    assert result.text == "page text"


def test_unknown_provider_fails_before_any_execution():
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        providers.parse_provider_config(SimpleNamespace(provider="codex-api", model=None), env={})

    assert "codex-api" in str(excinfo.value)
    assert "openai-api" in str(excinfo.value)


def _fake_claude_run(captured):
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        result = mock.Mock()
        result.returncode = 0
        result.stdout = "ok"
        result.stderr = ""
        return result
    return fake_run


def test_claude_cli_passes_configured_model(monkeypatch, tmp_path):
    # 2-1: a configured model must actually reach the CLI, not be silently
    # dropped (which made provenance metadata lie and two differently-modelled
    # readers indistinguishable).
    captured = {}
    monkeypatch.setattr(providers.subprocess, "run", _fake_claude_run(captured))
    provider = providers.ClaudeCliProvider(model_name="claude-opus-4-8", root=tmp_path)

    provider.compare_text("prompt", "ocr_compare_v0.1")

    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "claude-opus-4-8"


def test_claude_cli_omits_model_flag_for_sentinel(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(providers.subprocess, "run", _fake_claude_run(captured))
    provider = providers.ClaudeCliProvider(root=tmp_path)  # model_name defaults to "claude-cli"

    provider.compare_text("prompt", "ocr_compare_v0.1")

    assert "--model" not in captured["cmd"]


def test_claude_cli_child_env_strips_foreign_secrets(monkeypatch, tmp_path):
    # 2-4: a document-reading child must not carry OTHER providers' credentials.
    captured = {}
    monkeypatch.setattr(providers.subprocess, "run", _fake_claude_run(captured))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    provider = providers.ClaudeCliProvider(root=tmp_path)

    provider.transcribe_image(Path("page.png"), "prompt", "ocr_extraction_v0.1")

    child_env = captured["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in child_env
    assert "CODEX_API_KEY" not in child_env
    assert "HF_TOKEN" not in child_env
    # claude's own credential and ordinary vars survive.
    assert child_env["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert child_env["PATH"] == "/usr/bin"


def test_child_safe_env_basic_scrub(monkeypatch):
    for k in list(os.environ):
        if k.upper().endswith(providers._SECRET_ENV_SUFFIXES):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "keep")
    monkeypatch.setenv("HOME", "/home/u")
    env = providers._child_safe_env(keep_prefixes=("ANTHROPIC", "CLAUDE"))
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in env  # _TOKEN suffix
    assert env["ANTHROPIC_API_KEY"] == "keep"
    assert env["HOME"] == "/home/u"  # non-secret always kept


def test_child_safe_env_keep_prefixes_override_bedrock(monkeypatch):
    # claude-via-Bedrock: AWS_SECRET_ACCESS_KEY (ends _ACCESS_KEY) must survive
    # when the deployment opts it in.
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "drop")
    monkeypatch.setenv("HARNESS_CHILD_ENV_KEEP_PREFIXES", "aws,google")  # lowercase on purpose
    env = providers._child_safe_env(keep_prefixes=("ANTHROPIC", "CLAUDE"))
    assert env["AWS_SECRET_ACCESS_KEY"] == "aws-secret"  # kept via override (case-insensitive)
    assert "OPENAI_API_KEY" not in env  # still scrubbed


def test_child_safe_env_ignores_blank_override_entries(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "drop")
    monkeypatch.setenv("HARNESS_CHILD_ENV_KEEP_PREFIXES", " , ,")  # all blank
    env = providers._child_safe_env(keep_prefixes=("ANTHROPIC",))
    assert "OPENAI_API_KEY" not in env  # blank entries don't accidentally keep everything


def test_child_safe_env_non_secret_credential_paths_kept(monkeypatch):
    # These are NOT value-secrets (an ID / a file path) and don't match a secret
    # suffix, so they survive without any override -- Vertex needs the creds path,
    # Bedrock needs the key ID.
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/gcp.json")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA...")
    env = providers._child_safe_env(keep_prefixes=("ANTHROPIC",))
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/etc/gcp.json"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA..."


def test_openai_scan_intake_attaches_page_images(monkeypatch, tmp_path):
    # 2-2: the D2 scan on an HTTP provider must ATTACH images, not name file
    # paths the server cannot open.
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "r", "status": "completed", "output_text": "CLEAR"}'

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    pages = [tmp_path / "p1.png", tmp_path / "p2.png"]
    for p in pages:
        p.write_bytes(b"png")
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api", model_name="gpt-test"),
        env={"OPENAI_API_KEY": "secret"},
    )

    provider.scan_intake_content("scan prompt", "content_scan_v0.1", image_paths=pages)

    content = captured["payload"]["input"][0]["content"]
    image_parts = [c for c in content if c.get("type") == "input_image"]
    assert len(image_parts) == 2
    assert all(c["image_url"].startswith("data:image/png;base64,") for c in image_parts)


def test_openai_truncated_response_raises(monkeypatch):
    # 2-3: a token-capped, truncated response must not be returned as if it were
    # the whole page/redaction.
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return (
                b'{"id": "r", "status": "incomplete",'
                b' "incomplete_details": {"reason": "max_output_tokens"},'
                b' "output_text": "partial..."}'
            )

    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda request, timeout: FakeResponse())
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api", model_name="gpt-test"),
        env={"OPENAI_API_KEY": "secret"},
    )

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.classify_document("prompt", "classification_v0.1")
    assert "truncated" in str(excinfo.value).lower()


def test_openai_non_completed_status_raises(monkeypatch):
    # R4 hardening: any non-"completed" terminal status (e.g. "failed") is a
    # failure, not just "incomplete".
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "r", "status": "failed", "output_text": "junk"}'

    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda request, timeout: FakeResponse())
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api", model_name="gpt-test"),
        env={"OPENAI_API_KEY": "secret"},
    )
    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.classify_document("prompt", "classification_v0.1")
    assert "not completed" in str(excinfo.value).lower()


def test_claude_cli_fails_closed_on_empty_output(monkeypatch, tmp_path):
    # Failure-safety: exit 0 with empty stdout must NOT be returned as a valid
    # (empty) transcription -- a blank result is never content. It retries, then
    # raises.
    def fake_run(cmd, **kwargs):
        result = mock.Mock()
        result.returncode = 0
        result.stdout = "   \n"
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    with pytest.raises(providers.ProviderExecutionError):
        provider.compare_text("prompt", "ocr_compare_v0.1")


def test_claude_cli_error_detail_falls_back_to_stdout(monkeypatch, tmp_path):
    # Failure-safety: the CLI prints model/access errors to STDOUT, not stderr.
    # A non-zero exit with an empty stderr must still surface the stdout reason,
    # never an empty message.
    def fake_run(cmd, **kwargs):
        result = mock.Mock()
        result.returncode = 1
        result.stdout = "There's an issue with the selected model (bogus)."
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)
    provider = providers.ClaudeCliProvider(root=tmp_path)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.compare_text("prompt", "ocr_compare_v0.1")
    assert "selected model" in str(excinfo.value)


def test_codex_cli_fails_closed_on_empty_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        # codex writes its answer to --output-last-message; simulate an empty one.
        out_path = Path(cmd[cmd.index("--output-last-message") + 1])
        out_path.write_text("", encoding="utf-8")
        result = mock.Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.CodexCliProvider(root=tmp_path)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.compare_text("prompt", "ocr_compare_v0.1")
    assert "empty" in str(excinfo.value).lower()


def test_image_data_url_missing_file_raises_clean(tmp_path):
    # R5-A: a missing/unreadable page image must be a clean ProviderExecutionError,
    # not a raw FileNotFoundError out of the caller.
    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        providers._image_data_url(tmp_path / "does_not_exist.png")
    assert "could not read image" in str(excinfo.value)


def test_openai_scan_missing_image_raises_clean(monkeypatch, tmp_path):
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api", model_name="gpt-x"),
        env={"OPENAI_API_KEY": "k"},
    )
    with pytest.raises(providers.ProviderExecutionError):
        provider.scan_intake_content("s", "v", image_paths=[tmp_path / "missing.png"])


@pytest.mark.parametrize("empty", [None, []])
def test_scan_intake_requires_images(monkeypatch, tmp_path, empty):
    # R5-B: the D2 vision scan must never run blind (no images) -- all real
    # providers fail closed rather than silently scanning nothing.
    reals = [
        providers.ClaudeCliProvider(root=tmp_path),
        providers.CodexCliProvider(root=tmp_path),
        providers.build_provider(
            providers.ProviderConfig(provider_name="openai-api", model_name="gpt-x"),
            env={"OPENAI_API_KEY": "k"},
        ),
    ]
    for prov in reals:
        with pytest.raises(providers.ProviderExecutionError):
            prov.scan_intake_content("s", "v", image_paths=empty)


def test_openai_empty_output_rejected(monkeypatch):
    # F2: an empty output_text on a completed response must fail closed, matching
    # the CLI providers -- not be returned as a valid (empty) result.
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": "r", "status": "completed", "output_text": ""}'

    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda request, timeout: FakeResponse())
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openai-api", model_name="gpt-test"),
        env={"OPENAI_API_KEY": "secret"},
    )
    with pytest.raises(providers.ProviderExecutionError):
        provider.classify_document("prompt", "classification_v0.1")


# --- compare_text timeout override (policy-polarity long-passage support) ---


def test_compare_text_timeout_defaults_and_overrides():
    """The default stays 60s; a valid override is honoured."""
    assert providers.compare_text_timeout(env={}) == 60
    assert providers.compare_text_timeout(
        env={"HARNESS_LLM_COMPARE_TIMEOUT_SECONDS": "180"}) == 180


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "  "])
def test_compare_text_timeout_falls_back_on_invalid(bad):
    """A malformed timeout must not make every semantic analysis a hard error."""
    assert providers.compare_text_timeout(
        env={"HARNESS_LLM_COMPARE_TIMEOUT_SECONDS": bad}) == 60


def test_compare_text_passes_env_timeout_to_subprocess(monkeypatch, tmp_path):
    """The resolved timeout actually reaches the claude-cli subprocess call."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        result = mock.Mock()
        result.returncode = 0
        result.stdout = "AGREE"
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    monkeypatch.setenv("HARNESS_LLM_COMPARE_TIMEOUT_SECONDS", "240")
    providers.ClaudeCliProvider(root=tmp_path).compare_text(
        "compare prompt", "ocr_compare_v0.1")

    assert seen["timeout"] == 240


def test_codex_compare_text_passes_env_timeout_to_subprocess(monkeypatch, tmp_path):
    """Same override reaches codex-cli, the other production CLI analyzer.

    codex-cli returns its result via an output FILE (`--output-last-message`),
    not stdout, so the fake must populate that path or the provider correctly
    fails closed on empty output.
    """
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        for flag in ("--output-last-message", "--output_last_message"):
            if flag in cmd:
                Path(cmd[cmd.index(flag) + 1]).write_text("AGREE", encoding="utf-8")
        result = mock.Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    monkeypatch.setenv("HARNESS_LLM_COMPARE_TIMEOUT_SECONDS", "150")
    providers.CodexCliProvider(root=tmp_path).compare_text(
        "compare prompt", "ocr_compare_v0.1")

    assert seen["timeout"] == 150


# --------------------------------------------------------------------------
# Bounded CLI transport (Windows CreateProcess 32,767-char command-line limit).
#
# V4a's claim-analysis consolidation call built a 94,063-char argv (61,911
# prompt + 32,152 schema). CreateProcess failed with ERROR_FILE_NOT_FOUND,
# surfacing as FileNotFoundError and reported as "claude-cli command not
# found" for a binary that existed. These tests pin the fix: the prompt is
# never on the command line, and it reaches the child exactly once via stdin.
# --------------------------------------------------------------------------

WINDOWS_COMMAND_LINE_LIMIT = 32_767


def _capture_claude_run(monkeypatch, stdout="ok"):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        result = mock.Mock()
        result.returncode = 0
        result.stdout = stdout
        result.stderr = ""
        return result

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    return captured


def test_claude_cli_sends_prompt_on_stdin_not_argv(monkeypatch, tmp_path):
    captured = _capture_claude_run(monkeypatch, stdout="verdict")
    prompt = "compare these two readings carefully"

    providers.ClaudeCliProvider(root=tmp_path).compare_text(prompt, "ocr_compare_v0.1")

    cmd = captured["cmd"]
    # -p is still passed; the prompt body is not an argument to it.
    assert "-p" in cmd
    assert prompt not in cmd
    assert not any(prompt in str(part) for part in cmd)
    # Delivered exactly once, verbatim, over stdin.
    assert captured["kwargs"]["input"] == prompt
    assert sum(1 for part in cmd if part == prompt) == 0
    # input= and stdin= are mutually exclusive in subprocess.run; passing both
    # raises, so the old DEVNULL must be gone rather than merely unused.
    assert "stdin" not in captured["kwargs"]
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["encoding"] == "utf-8"


def test_claude_cli_long_korean_prompt_stays_off_command_line(monkeypatch, tmp_path):
    """A prompt larger than the whole Windows command line must still run.

    Korean is the realistic case: the consolidation prompt is Korean claim
    text, and this is the exact shape that made V4a fail structurally.
    """
    captured = _capture_claude_run(
        monkeypatch,
        stdout=json.dumps({"subtype": "success", "is_error": False,
                           "structured_output": {"ok": True}}),
    )
    long_prompt = "손해사정 청구 사실관계 확인 " * 4_000
    assert len(long_prompt) > WINDOWS_COMMAND_LINE_LIMIT

    providers.ClaudeCliProvider(root=tmp_path).analyze_text_structured(
        long_prompt, "claim_analysis_v0.1", {"type": "object"})

    cmd = captured["cmd"]
    assert all(long_prompt not in str(part) for part in cmd)
    assert captured["kwargs"]["input"] == long_prompt
    # The whole constructed command line stays far below the OS limit.
    assert sum(len(str(part)) + 1 for part in cmd) < WINDOWS_COMMAND_LINE_LIMIT


def test_claude_cli_rejects_oversized_inline_schema_before_spawn(monkeypatch, tmp_path):
    """A large schema must not be misreported as a missing executable on Windows."""
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("oversized schema must fail before subprocess.run")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    schema = {"type": "object", "properties": {"payload": {"type": "string", "description": "x" * 9_000}}}

    with pytest.raises(providers.ProviderExecutionError, match="schema is too large"):
        providers.ClaudeCliProvider(root=tmp_path).analyze_text_structured(
            "extract", "transport_limit_v0.1", schema)

    assert called is False


def test_structured_text_timeout_is_independent_of_compare_timeout(monkeypatch):
    monkeypatch.delenv("HARNESS_STRUCTURED_TEXT_TIMEOUT", raising=False)
    monkeypatch.delenv("HARNESS_LLM_COMPARE_TIMEOUT_SECONDS", raising=False)

    assert providers.structured_text_timeout() == 180
    assert providers.compare_text_timeout() == 60

    # Raising one must not move the other -- the point of splitting them.
    monkeypatch.setenv("HARNESS_LLM_COMPARE_TIMEOUT_SECONDS", "300")
    assert providers.structured_text_timeout() == 180

    monkeypatch.setenv("HARNESS_STRUCTURED_TEXT_TIMEOUT", "240")
    assert providers.structured_text_timeout() == 240
    assert providers.compare_text_timeout() == 300


@pytest.mark.parametrize("bad", ["", "   ", "abc", "0", "-5", "12.5", "99999999"])
def test_structured_text_timeout_rejects_bad_values_safely(monkeypatch, bad):
    """Malformed/zero/negative/out-of-range all fall back, never raise.

    An implausibly large value is refused too: a child that effectively never
    times out is indistinguishable from a hang.
    """
    monkeypatch.setenv("HARNESS_STRUCTURED_TEXT_TIMEOUT", bad)
    assert providers.structured_text_timeout() == 180


def test_analyze_text_structured_uses_structured_timeout(monkeypatch, tmp_path):
    captured = _capture_claude_run(
        monkeypatch,
        stdout=json.dumps({"subtype": "success", "is_error": False,
                           "structured_output": {"ok": True}}),
    )
    monkeypatch.setenv("HARNESS_STRUCTURED_TEXT_TIMEOUT", "222")
    monkeypatch.setenv("HARNESS_LLM_COMPARE_TIMEOUT_SECONDS", "60")

    providers.ClaudeCliProvider(root=tmp_path).analyze_text_structured(
        "extract facts", "claim_analysis_v0.1", {"type": "object"})

    assert captured["kwargs"]["timeout"] == 222


def test_cli_schema_sanitizer_strips_only_documented_keywords():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "nested": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "dependentRequired": {"a": ["b"]},
                "type": "object",
            },
        },
        "dependentRequired": {"a": ["b"]},
        "required": ["a"],
        "additionalProperties": False,
    }
    original = json.loads(json.dumps(schema))

    cleaned = providers._cli_json_schema(schema)

    # Both documented keywords removed, at every depth.
    assert "$schema" not in cleaned
    assert "dependentRequired" not in cleaned
    assert "$schema" not in cleaned["properties"]["nested"]
    assert "dependentRequired" not in cleaned["properties"]["nested"]
    # Everything else survives untouched.
    assert cleaned["type"] == "object"
    assert cleaned["required"] == ["a"]
    assert cleaned["additionalProperties"] is False
    assert cleaned["properties"]["a"] == {"type": "string"}
    assert cleaned["properties"]["nested"]["type"] == "object"
    # The authoritative input schema is never mutated in place.
    assert schema == original


def _fake_anthropic_response(body: bytes):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return body

    return FakeResponse()


def test_anthropic_provider_forces_tool_use_with_cached_prompt(monkeypatch):
    captured = {}
    response = json.dumps({
        "id": "msg_1",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "name": "emit_result", "input": {"ok": True}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 0},
    }).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _fake_anthropic_response(response)

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("HARNESS_STRUCTURED_TEXT_TIMEOUT", raising=False)
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="anthropic-api", model_name="claude-test"),
        env={"ANTHROPIC_API_KEY": "secret"},
    )

    result = provider.analyze_text_structured(
        "analyze this", "driver_v1",
        {"$schema": "https://json-schema.org/draft/2020-12/schema",
         "type": "object", "properties": {"ok": {"type": "boolean"}}},
    )

    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["X-api-key"] == "secret"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    payload = captured["payload"]
    assert payload["model"] == "claude-test"
    assert payload["max_tokens"] == providers._ANTHROPIC_MAX_TOKENS_DEFAULT
    assert payload["tool_choice"] == {"type": "tool", "name": "emit_result"}
    tool = payload["tools"][0]
    assert tool["name"] == "emit_result"
    # The CLI keyword strip applies here too, keeping the two transports
    # byte-comparable ($schema would otherwise ride to the API for nothing).
    assert "$schema" not in tool["input_schema"]
    assert tool["input_schema"]["properties"] == {"ok": {"type": "boolean"}}
    block = payload["messages"][0]["content"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["text"] == "analyze this"
    assert captured["timeout"] == 180
    assert result.structured_output == {"ok": True}
    assert json.loads(result.text) == {"ok": True}
    assert result.raw_metadata["usage"]["cache_read_input_tokens"] == 0
    assert result.finish_reason == "tool_use"


def test_anthropic_provider_refuses_max_tokens_truncation(monkeypatch):
    # A truncated response can still carry a tool_use block whose input is
    # silently partial -- stop_reason is the only honest signal, so it must
    # win even when a parseable input is present.
    response = json.dumps({
        "id": "msg_2",
        "stop_reason": "max_tokens",
        "content": [
            {"type": "tool_use", "name": "emit_result", "input": {"ok": True}},
        ],
    }).encode("utf-8")
    monkeypatch.setattr(
        providers.urllib.request, "urlopen",
        lambda request, timeout: _fake_anthropic_response(response),
    )
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="anthropic-api", model_name="claude-test"),
        env={"ANTHROPIC_API_KEY": "secret"},
    )

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.analyze_text_structured("p", "v", {"type": "object"})

    assert "truncated at max_tokens" in str(excinfo.value)
    assert "HARNESS_ANTHROPIC_MAX_TOKENS" in str(excinfo.value)


def test_anthropic_provider_refuses_text_only_response(monkeypatch):
    response = json.dumps({
        "id": "msg_3",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "prose, not the tool"}],
    }).encode("utf-8")
    monkeypatch.setattr(
        providers.urllib.request, "urlopen",
        lambda request, timeout: _fake_anthropic_response(response),
    )
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="anthropic-api", model_name="claude-test"),
        env={"ANTHROPIC_API_KEY": "secret"},
    )

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.analyze_text_structured("p", "v", {"type": "object"})

    assert "structured tool_use" in str(excinfo.value)


def test_anthropic_provider_max_tokens_env_must_be_a_positive_integer():
    with pytest.raises(providers.ProviderConfigError):
        providers.AnthropicApiProvider(
            model_name="claude-test",
            env={"ANTHROPIC_API_KEY": "secret",
                 "HARNESS_ANTHROPIC_MAX_TOKENS": "many"},
        )
    with pytest.raises(providers.ProviderConfigError):
        providers.AnthropicApiProvider(
            model_name="claude-test",
            env={"ANTHROPIC_API_KEY": "secret",
                 "HARNESS_ANTHROPIC_MAX_TOKENS": "0"},
        )
    provider = providers.AnthropicApiProvider(
        model_name="claude-test",
        env={"ANTHROPIC_API_KEY": "secret",
             "HARNESS_ANTHROPIC_MAX_TOKENS": "32000"},
    )
    assert provider.max_output_tokens == 32000
