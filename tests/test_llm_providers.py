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
        "codex", "exec", "transcribe prompt", "--skip-git-repo-check",
        "--sandbox", "read-only", "--model",
    ]
    assert captured["cmd"][7:11] == ["gpt-test", "--image", "page.png", "--output-last-message"]
    output_path = Path(captured["cmd"][11])
    assert not output_path.exists()
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["timeout"] == 180
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
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


def test_local_ocr_runs_tesseract_without_network(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="추출된 텍스트", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.build_provider(
        providers.ProviderConfig("local-ocr", "kor+eng:11"),
        env={"HARNESS_LOCAL_OCR_COMMAND": "tesseract-local"},
        root=tmp_path,
    )

    result = provider.transcribe_image(Path("page.png"), "ignored", "ocr_v1")

    assert captured["cmd"] == [
        "tesseract-local", "page.png", "stdout", "-l", "kor+eng", "--psm", "11"
    ]
    assert "input" not in captured["kwargs"]
    assert result.text == "추출된 텍스트"
    assert result.provider_name == "local-ocr"


def test_local_ocr_never_falls_back_when_command_is_missing(monkeypatch):
    monkeypatch.setattr(providers.subprocess, "run", mock.Mock(side_effect=FileNotFoundError("missing")))
    provider = providers.LocalOcrProvider(command="missing-tesseract")

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.transcribe_image(Path("page.png"), "prompt", "ocr_v1")

    assert "install Tesseract" in str(excinfo.value)


def test_local_vlm_rejects_non_loopback_ollama_host():
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        providers.LocalVlmProvider(
            model_name="qwen3-vl:4b", env={"OLLAMA_HOST": "https://example.com"}
        )

    assert "loopback" in str(excinfo.value)


def test_local_vlm_verifies_model_and_posts_base64_image(monkeypatch, tmp_path):
    calls = []
    captured = {}

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="model exists", stderr="")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"response":"page text","done":true,"total_duration":123}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake png")
    provider = providers.build_provider(
        providers.ProviderConfig("local-vlm", "qwen3-vl:4b"),
        env={
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OLLAMA_MODELS": "E:/runtime/models",
            "HARNESS_LOCAL_VLM_COMMAND": "E:/runtime/ollama.exe",
        },
    )

    result = provider.transcribe_image(image_path, "transcribe", "ocr_v1")

    assert calls[0][0] == ["E:/runtime/ollama.exe", "show", "qwen3-vl:4b"]
    assert calls[0][1]["env"]["OLLAMA_MODELS"] == "E:/runtime/models"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["payload"]["model"] == "qwen3-vl:4b"
    assert captured["payload"]["images"] == ["ZmFrZSBwbmc="]
    assert captured["payload"]["stream"] is False
    assert captured["timeout"] == 300
    assert result.text == "page text"


def test_local_llm_rejects_non_loopback_ollama_host():
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        providers.LocalLlmProvider(
            model_name="local-model", env={"OLLAMA_HOST": "https://example.com"}
        )

    assert "loopback" in str(excinfo.value)


def test_local_llm_verifies_preloaded_model_before_run(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[1] == "show":
            return SimpleNamespace(returncode=0, stdout="model info", stderr="")
        return SimpleNamespace(returncode=0, stdout="AGREE: same", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.LocalLlmProvider(model_name="local-model", env={})

    result = provider.compare_text("compare prompt", "compare_v1")

    assert calls[0][0] == ["ollama", "show", "local-model"]
    assert calls[1][0] == ["ollama", "run", "local-model"]
    assert calls[1][1]["input"] == "compare prompt"
    assert calls[1][1]["env"]["OLLAMA_HOST"] == "http://127.0.0.1:11434"
    assert result.text == "AGREE: same"


def test_local_llm_refuses_to_auto_download_missing_model(monkeypatch):
    monkeypatch.setattr(
        providers.subprocess,
        "run",
        mock.Mock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="missing")),
    )
    provider = providers.LocalLlmProvider(model_name="missing-model", env={})

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.classify_document("prompt", "classification_v1")

    assert "automatic download is disabled" in str(excinfo.value)
