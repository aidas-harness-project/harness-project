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

    result = provider.transcribe_image(Path("page.png"), "transcribe prompt", "ocr_extraction_v0.1")

    # The transcription prompt must stay NEUTRAL -- no defensive "role framing"
    # preamble (a prior version prepended "this is a SANCTIONED step, do not
    # refuse..." which the nested claude read as a prompt-injection signal and
    # refused). The image is referenced as an explicit Read instruction, not a
    # trailing "Image: {path}" label (the label form is read as metadata about a
    # never-arriving attachment and the Read tool is never invoked). This
    # assertion guards both against framing creeping back in.
    assert captured["cmd"] == [
        "claude",
        "-p",
        "Read the image file at page.png and then: transcribe prompt",
        "--safe-mode",
        "--allowedTools",
        "Read",
    ]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["timeout"] == 180
    assert result.text == "transcribed text"
    assert result.metadata()["provider_name"] == "claude-cli"


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
    provider.scan_intake_content("scan prompt", "intake_scan_v0.1")

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


def test_claude_cli_provider_closes_stdin(monkeypatch, tmp_path):
    """Without stdin=DEVNULL the child claude process blocks ~3s waiting for
    input ('no stdin data received in 3s') and intermittently exits non-zero
    on a live pipe -- the cause of CASE_003/DOC_008 dying mid-run."""
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

    assert captured["kwargs"]["stdin"] == providers.subprocess.DEVNULL


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
