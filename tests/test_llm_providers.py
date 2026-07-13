from pathlib import Path
import json
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
    config = providers.ProviderConfig(provider_name="anthropic-api")
    provider = providers.build_provider(config, env={"ANTHROPIC_API_KEY": "secret"})

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.classify_document("prompt", "classification_v0.1")

    assert "anthropic-api" in str(excinfo.value)
    assert "not implemented" in str(excinfo.value)


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

    result = provider.transcribe_image(Path("page.png"), "transcribe prompt", "ocr_extraction_v0.1")

    assert captured["cmd"] == [
        "claude",
        "-p",
        "transcribe prompt\n\nImage: page.png",
        "--allowedTools",
        "Read",
    ]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["timeout"] == 180
    assert result.text == "transcribed text"
    assert result.metadata()["provider_name"] == "claude-cli"


def test_claude_cli_provider_reports_missing_command(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider(root=tmp_path, command="missing-claude")

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.compare_text("compare prompt", "ocr_compare_v0.1")

    assert "claude-cli command not found: missing-claude" in str(excinfo.value)


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
