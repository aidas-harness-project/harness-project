"""OpenRouter provider: transport shape and the fail-closed gates.

Every test drives a fake ``urlopen`` -- nothing here reaches the network. The
gates matter more than the happy path: this provider is the DEFAULT, so a
truncated, blocked, or empty completion that it lets through lands directly in
the trusted processed layer.
"""
import email.message
import io
import json
from types import SimpleNamespace

import pytest
import urllib.error

import llm_providers as providers


ENV = {"OPENROUTER_API_KEY": "secret", "OPENROUTER_MODEL": "vendor/model-test"}


@pytest.fixture
def no_backoff_wait(monkeypatch):
    """Record each computed backoff without serving the wait.

    Deliberately NOT `monkeypatch.setattr(providers.time, "sleep", ...)`:
    `providers.time` IS the stdlib module, so stubbing its `sleep` silences
    sleeping process-wide rather than only inside the code under test.
    """
    delays = []
    real = providers._retry_delay

    def spy(attempt, detail=""):
        delays.append(real(attempt, detail))
        return 0.0

    monkeypatch.setattr(providers, "_retry_delay", spy)
    return delays


def build(env=None):
    return providers.build_provider(
        providers.ProviderConfig(provider_name="openrouter"), env=env or ENV)


class FakeResponse:
    status = 200

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def completion(content="page text", *, finish_reason="stop", **extra):
    body = {
        "id": "gen-1",
        "model": "vendor/model-served",
        "provider": "Upstream",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
            "native_finish_reason": "end_turn",
        }],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }
    body.update(extra)
    return json.dumps(body)


def install(monkeypatch, body, captured=None):
    def fake_urlopen(request, timeout):
        if captured is not None:
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
        return FakeResponse(body)

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)


def http_error(code, body="{}", retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://openrouter.ai/api/v1/chat/completions", code, "err", headers,
        io.BytesIO(body.encode("utf-8")),
    )


# ---------------------------------------------------------------- transport --

def test_text_call_posts_openai_chat_shape(monkeypatch):
    captured = {}
    install(monkeypatch, completion("AGREE: same"), captured)

    result = build().compare_text("compare prompt", "ocr_compare_v0.1")

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"]["model"] == "vendor/model-test"
    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "compare prompt"}]
    assert captured["timeout"] == 60
    assert result.text == "AGREE: same"
    assert result.finish_reason == "stop"
    # The model that ACTUALLY served the call, not the slug we asked for --
    # OpenRouter may route to a fallback and provenance must record what ran.
    assert result.raw_metadata["served_model"] == "vendor/model-served"
    assert result.raw_metadata["usage"]["total_tokens"] == 14


def test_image_call_uses_chat_completions_image_part(monkeypatch, tmp_path):
    captured = {}
    install(monkeypatch, completion(), captured)
    image = tmp_path / "page.png"
    image.write_bytes(b"fake png")

    build().transcribe_image(image, "transcribe prompt", "ocr_extraction_v0.1")

    content = captured["payload"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "transcribe prompt"}
    # NOT the Responses-API `input_image` shape: a chat-completions endpoint
    # rejects that, and the silent version of the mistake is a request that
    # carries no image at all.
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert captured["timeout"] == 180


def test_scan_intake_attaches_every_page(monkeypatch, tmp_path):
    captured = {}
    install(monkeypatch, completion("no ground truth found"), captured)
    pages = []
    for name in ("p1.png", "p2.png"):
        page = tmp_path / name
        page.write_bytes(b"fake png")
        pages.append(page)

    build().scan_intake_content("scan prompt", "d2_scan_v0.1", pages)

    content = captured["payload"]["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text", "image_url", "image_url"]


def test_scan_intake_without_images_fails_closed():
    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().scan_intake_content("scan prompt", "d2_scan_v0.1", [])

    assert "cannot run blind" in str(excinfo.value)


def test_attribution_headers_only_sent_when_configured(monkeypatch):
    captured = {}
    install(monkeypatch, completion(), captured)
    build().classify_document("classify", "classify_v0.1")
    assert "Http-Referer" not in captured["headers"]

    install(monkeypatch, completion(), captured)
    build({**ENV, "HARNESS_OPENROUTER_REFERER": "https://example.test",
           "HARNESS_OPENROUTER_TITLE": "harness"}).classify_document(
        "classify", "classify_v0.1")
    assert captured["headers"]["Http-referer"] == "https://example.test"
    assert captured["headers"]["X-title"] == "harness"


# -------------------------------------------------------------- structured --

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
}


def tool_completion(arguments, *, name="emit_result"):
    return json.dumps({
        "id": "gen-2",
        "model": "vendor/model-served",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })


def test_structured_call_forces_one_tool_and_returns_its_arguments(monkeypatch):
    captured = {}
    install(monkeypatch, tool_completion('{"verdict": "AGREE"}'), captured)

    result = build().analyze_text_structured("prompt", "v0.1", SCHEMA)

    function = captured["payload"]["tools"][0]["function"]
    assert function["name"] == "emit_result"
    assert captured["payload"]["tool_choice"] == {
        "type": "function", "function": {"name": "emit_result"}}
    # $schema is stripped for the same reason the CLI path strips it -- the
    # authoritative gate is validate_instance() against the on-disk schema.
    assert "$schema" not in function["parameters"]
    assert function["parameters"]["required"] == ["verdict"]
    assert result.structured_output == {"verdict": "AGREE"}
    assert json.loads(result.text) == {"verdict": "AGREE"}


def test_structured_call_accepts_already_decoded_arguments(monkeypatch):
    # Some upstream providers hand OpenRouter a decoded object rather than the
    # JSON string the OpenAI contract specifies; it passes through as one.
    install(monkeypatch, tool_completion({"verdict": "DISAGREE"}))

    result = build().analyze_text_structured("prompt", "v0.1", SCHEMA)

    assert result.structured_output == {"verdict": "DISAGREE"}


def test_structured_call_fails_closed_without_the_tool_call(monkeypatch):
    install(monkeypatch, completion('{"verdict": "AGREE"}'))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().analyze_text_structured("prompt", "v0.1", SCHEMA)

    # Prose that merely CONTAINS JSON is not a structured result.
    assert "did not return the forced emit_result tool call" in str(excinfo.value)


def test_structured_call_fails_closed_on_unparseable_arguments(monkeypatch):
    install(monkeypatch, tool_completion('{"verdict": '))

    with pytest.raises(providers.ProviderExecutionError):
        build().analyze_text_structured("prompt", "v0.1", SCHEMA)


def test_structured_image_call_carries_both_image_and_tool(monkeypatch, tmp_path):
    captured = {}
    install(monkeypatch, tool_completion('{"verdict": "AGREE"}'), captured)
    image = tmp_path / "page.png"
    image.write_bytes(b"fake png")

    build().analyze_image_structured(image, "prompt", "v0.1", SCHEMA)

    content = captured["payload"]["messages"][0]["content"]
    assert content[1]["type"] == "image_url"
    assert captured["payload"]["tool_choice"]["function"]["name"] == "emit_result"


# ------------------------------------------------------------- fail closed --

@pytest.mark.parametrize("finish_reason", ["length", "error", "content_filter"])
def test_unusable_finish_reason_refuses_the_result(monkeypatch, finish_reason):
    install(monkeypatch, completion("half a pa", finish_reason=finish_reason))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().transcribe_image(__import__("pathlib").Path(__file__), "p", "v0.1")

    assert finish_reason in str(excinfo.value)


def test_error_object_in_a_200_body_is_surfaced(monkeypatch):
    # OpenRouter can answer 200 OK with a top-level error (an upstream failure
    # after the response was committed, a moderation block). `choices` is then
    # absent -- the error text is the diagnostic worth reporting.
    install(monkeypatch, json.dumps(
        {"error": {"code": 502, "message": "upstream provider is down"}}))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().redact_text("redact", "v0.1")

    assert "upstream provider is down" in str(excinfo.value)


def test_empty_content_is_never_accepted(monkeypatch):
    install(monkeypatch, completion("   "))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().redact_text("redact", "v0.1")

    assert "no usable message content" in str(excinfo.value)


def test_missing_choices_is_reported_not_indexed(monkeypatch):
    install(monkeypatch, json.dumps({"id": "gen-3", "choices": []}))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().classify_document("classify", "v0.1")

    assert "no choices" in str(excinfo.value)


def test_non_json_body_is_reported(monkeypatch):
    install(monkeypatch, "<html>gateway timeout</html>")

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().classify_document("classify", "v0.1")

    assert "non-JSON response" in str(excinfo.value)


# ------------------------------------------------------------------ retry --

def test_rate_limit_is_retried_then_succeeds(monkeypatch, no_backoff_wait):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise http_error(429, '{"error": {"message": "rate limited"}}', retry_after=3)
        return FakeResponse(completion("page text"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    result = build().classify_document("classify", "v0.1")

    assert result.text == "page text"
    assert len(calls) == 2
    # The server's Retry-After is a floor on the wait, jitter on top.
    assert no_backoff_wait and no_backoff_wait[0] >= 3


def test_client_error_is_not_retried(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise http_error(400, '{"error": {"message": "bad model slug"}}')

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().classify_document("classify", "v0.1")

    # A 400 is a configuration fault; re-sending it burns quota and cannot fix it.
    assert len(calls) == 1
    assert "bad model slug" in str(excinfo.value)


def test_retries_are_exhausted_and_the_last_failure_raised(monkeypatch, no_backoff_wait):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise http_error(503, '{"error": {"message": "no instances available"}}')

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().classify_document("classify", "v0.1")

    assert len(calls) == providers._OPENROUTER_MAX_ATTEMPTS
    assert "no instances available" in str(excinfo.value)


def test_structured_calls_retry_on_transport_failures_too(monkeypatch, no_backoff_wait):
    # Unlike claude-cli (which refuses to retry a structured call because a
    # non-zero exit cannot be told apart from a schema rejection), an HTTP
    # status proves no completion was produced -- so no model output is
    # discarded and re-rolled, and the caller's single P4 correction is intact.
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise http_error(502, "{}")
        return FakeResponse(tool_completion('{"verdict": "AGREE"}'))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    result = build().analyze_text_structured("prompt", "v0.1", SCHEMA)

    assert result.structured_output == {"verdict": "AGREE"}
    assert len(calls) == 2


# ------------------------------------------------------------------ config --

def test_missing_api_key_fails_at_build_not_at_call():
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        providers.build_provider(
            providers.ProviderConfig(provider_name="openrouter"), env={})

    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_missing_model_fails_at_build_not_at_call():
    # No fabricated default slug: OpenRouter model ids are vendor-prefixed and
    # a made-up one only fails later as an opaque 404 on the first page.
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        providers.build_provider(
            providers.ProviderConfig(provider_name="openrouter"),
            env={"OPENROUTER_API_KEY": "secret"})

    assert "requires a model name" in str(excinfo.value)


def test_cli_model_flag_wins_over_environment():
    config = providers.parse_provider_config(
        SimpleNamespace(provider="openrouter", model="vendor/flag-model"),
        env={"HARNESS_LLM_MODEL": "vendor/env-model"})
    provider = providers.build_provider(config, env=ENV)

    assert provider.model_name == "vendor/flag-model"


def test_max_tokens_override_is_validated():
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openrouter"),
        env={**ENV, "HARNESS_OPENROUTER_MAX_TOKENS": "32000"})
    assert provider.max_output_tokens == 32000

    with pytest.raises(providers.ProviderConfigError):
        providers.build_provider(
            providers.ProviderConfig(provider_name="openrouter"),
            env={**ENV, "HARNESS_OPENROUTER_MAX_TOKENS": "0"})

    with pytest.raises(providers.ProviderConfigError):
        providers.build_provider(
            providers.ProviderConfig(provider_name="openrouter"),
            env={**ENV, "HARNESS_OPENROUTER_MAX_TOKENS": "lots"})


def test_base_url_override(monkeypatch):
    captured = {}
    install(monkeypatch, completion(), captured)

    build({**ENV, "OPENROUTER_BASE_URL": "https://proxy.test/v1/"}).classify_document(
        "classify", "v0.1")

    assert captured["url"] == "https://proxy.test/v1/chat/completions"
