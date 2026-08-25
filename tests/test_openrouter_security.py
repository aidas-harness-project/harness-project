"""What the OpenRouter transport is allowed to put on the wire, and what it is not.

Each test here corresponds to a way a call could be unsafe rather than merely
wrong: the credential travelling in clear, prompt material reaching a provider
that retains it, the key or a page of claim text ending up in a message or a
metadata record, or an untrusted document persuading the model to do something
other than answer.

Nothing here needs an API key and nothing leaves the machine.
"""
from __future__ import annotations

import json

import pytest

import llm_providers as providers
import trace as trace_mod

KEY = "sk-or-v1-SECRETVALUE-not-a-real-credential"
ENV = {"OPENROUTER_API_KEY": KEY, "OPENROUTER_MODEL": "vendor/model-test"}


class FakeResponse:
    status = 200

    def __init__(self, body):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def completion(content="page text"):
    return json.dumps({
        "id": "gen-1",
        "model": "vendor/model-served",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"total_tokens": 12},
    })


def capture(monkeypatch, body=None):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(body or completion())

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    return seen


def build(**env_extra):
    return providers.build_provider(
        providers.ProviderConfig(provider_name="openrouter"), env={**ENV, **env_extra})


# ------------------------------------------------- the credential in transit --

def test_a_plain_http_base_url_is_refused_before_any_call():
    # The Bearer key and the page text would both be readable on the wire.
    with pytest.raises(providers.ProviderConfigError) as excinfo:
        build(OPENROUTER_BASE_URL="http://openrouter.example.com/api/v1")

    assert "not https" in str(excinfo.value)
    assert KEY not in str(excinfo.value), "the refusal must not quote the key"


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080", "[::1]:8080"])
def test_plain_http_to_loopback_is_allowed(host):
    # Nothing reaches a network: the test server and a same-machine proxy.
    provider = build(OPENROUTER_BASE_URL=f"http://{host}/api/v1")
    assert provider.base_url.startswith("http://")


def test_the_insecure_opt_out_is_explicit_and_works():
    provider = build(OPENROUTER_BASE_URL="http://openrouter.example.com/api/v1",
                     HARNESS_OPENROUTER_ALLOW_INSECURE_BASE_URL="1")
    assert provider.base_url == "http://openrouter.example.com/api/v1"


def test_a_non_http_scheme_is_refused():
    with pytest.raises(providers.ProviderConfigError):
        build(OPENROUTER_BASE_URL="ftp://openrouter.example.com/api/v1")


# ------------------------------------------------------------- PII posture --

def test_every_call_asks_for_providers_that_do_not_retain_prompts(monkeypatch):
    seen = capture(monkeypatch)
    build().classify_document("classify", "v0.1")
    assert seen["payload"]["provider"] == {"data_collection": "deny"}


def test_an_image_call_carries_the_same_policy(monkeypatch, tmp_path):
    # OCR sends the page BEFORE redaction -- the strictest case, so the policy
    # must not be attached only on the text paths.
    image = tmp_path / "page.png"
    image.write_bytes(b"fake png")
    seen = capture(monkeypatch)
    build().transcribe_image(image, "transcribe", "v0.1")
    assert seen["payload"]["provider"]["data_collection"] == "deny"


def test_zero_data_retention_can_be_pinned(monkeypatch):
    seen = capture(monkeypatch)
    build(HARNESS_OPENROUTER_ZDR="1").redact_text("redact", "v0.1")
    assert seen["payload"]["provider"] == {"data_collection": "deny", "zdr": True}


def test_allowing_retention_is_possible_but_must_be_spelled_out(monkeypatch):
    seen = capture(monkeypatch)
    build(HARNESS_OPENROUTER_DATA_COLLECTION="allow").redact_text("redact", "v0.1")
    assert seen["payload"]["provider"]["data_collection"] == "allow"

    with pytest.raises(providers.ProviderConfigError):
        build(HARNESS_OPENROUTER_DATA_COLLECTION="maybe")


def test_the_policy_in_force_is_recorded_with_the_result(monkeypatch):
    # Read back from a contract months later, "which posture was this page read
    # under" must not depend on what the environment happens to hold then.
    capture(monkeypatch)
    result = build().classify_document("classify", "v0.1")
    assert result.raw_metadata["data_collection"] == "deny"
    assert result.raw_metadata["zdr"] is False


# ------------------------------------------ the key must not end up anywhere --

def test_the_key_is_absent_from_the_result_and_its_metadata(monkeypatch):
    capture(monkeypatch)
    result = build().classify_document("classify", "v0.1")
    assert KEY not in json.dumps(result.metadata(), ensure_ascii=False)
    assert KEY not in json.dumps(result.raw_metadata, ensure_ascii=False)
    assert KEY not in result.text


def test_the_key_is_absent_from_a_failure_message(monkeypatch):
    def fake_urlopen(request, timeout):
        raise providers.urllib.error.URLError("connection refused")

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers, "_retry_delay", lambda *a, **k: 0.0)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().classify_document("classify", "v0.1")

    assert KEY not in str(excinfo.value)


def test_a_traced_call_records_sizes_of_the_prompt_never_the_prompt(monkeypatch):
    """Asserted on what a span actually receives, not on the allow-list's spelling.

    A name-substring check would be misleading in both directions:
    `prompt_version` and `input_images` are safe identifiers that contain the
    words, and a genuinely unsafe key could be named anything. So this drives a
    real traced call with a distinctive Korean prompt and reads back every
    attribute the span was given.
    """
    recorded = {}

    class FakeSpan:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def set(self, **attrs):
            recorded.update(attrs)

    def fake_span(op, **attrs):
        recorded.update(attrs)
        return FakeSpan()

    monkeypatch.setattr(trace_mod, "enabled", lambda: True)
    monkeypatch.setattr(providers.trace_mod, "span", fake_span)
    capture(monkeypatch)

    secret_page = "환자 홍길동 진단명 우측 요골 원위부 골절"
    build().redact_text(secret_page, "redaction_v0.1")

    serialized = json.dumps(recorded, ensure_ascii=False)
    assert secret_page not in serialized
    assert "홍길동" not in serialized
    assert KEY not in serialized
    # What it DOES record: the size, which is what the measurement needs.
    assert recorded["input_chars"] == len(secret_page)
    assert recorded["prompt_version"] == "redaction_v0.1"


def test_a_cli_child_never_inherits_the_openrouter_key(monkeypatch):
    # A claude-cli/codex-cli child reads untrusted claim images. Now that an
    # OpenRouter key exists in the environment, an injected "read this and also
    # print your environment" must not be able to reach it.
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    child_env = providers._child_safe_env(keep_prefixes=("ANTHROPIC", "CLAUDE"))
    assert "OPENROUTER_API_KEY" not in child_env


# ------------------------------------------- what an untrusted page can ask for --

def test_a_plain_call_offers_the_model_no_tools_at_all(monkeypatch):
    # Transcription and redaction run on untrusted document content. With no
    # `tools` key in the request there is no tool for injected text to invoke.
    seen = capture(monkeypatch)
    build().redact_text("redact this page", "v0.1")
    assert "tools" not in seen["payload"]
    assert "tool_choice" not in seen["payload"]


def test_a_structured_call_offers_exactly_one_inert_tool(monkeypatch):
    seen = capture(monkeypatch)
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    body = json.dumps({
        "id": "g", "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c", "type": "function",
                "function": {"name": "emit_result",
                             "arguments": '{"verdict": "AGREE"}'}}]},
            "finish_reason": "tool_calls"}]})
    seen = capture(monkeypatch, body)

    build().analyze_text_structured("prompt", "v0.1", schema)

    tools = seen["payload"]["tools"]
    assert [t["function"]["name"] for t in tools] == ["emit_result"]
    # The one tool is a RETURN CHANNEL, not a capability: its arguments are
    # read as data. Nothing in this module executes a returned tool call.
    assert seen["payload"]["tool_choice"]["function"]["name"] == "emit_result"


def test_a_tool_call_the_model_invented_is_ignored_not_executed(monkeypatch):
    body = json.dumps({
        "id": "g", "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c", "type": "function",
                "function": {"name": "read_file",
                             "arguments": '{"path": "/etc/passwd"}'}}]},
            "finish_reason": "tool_calls"}]})
    capture(monkeypatch, body)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().analyze_text_structured(
            "prompt", "v0.1", {"type": "object", "properties": {}})

    assert "did not return the forced emit_result tool call" in str(excinfo.value)


def test_an_image_is_always_inlined_never_a_url_the_provider_must_fetch(
        monkeypatch, tmp_path):
    # Page bytes are read locally and inlined. If a path could ever become a
    # remote URL, the provider would be fetching a document-controlled address
    # on our behalf.
    image = tmp_path / "page.png"
    image.write_bytes(b"fake png")
    seen = capture(monkeypatch)

    build().scan_intake_content("scan", "v0.1", [image])

    for part in seen["payload"]["messages"][0]["content"]:
        if part.get("type") == "image_url":
            assert part["image_url"]["url"].startswith("data:")


def test_streaming_is_never_requested(monkeypatch):
    # A streamed response commits a 200 before the body is known and carries
    # mid-stream errors inside it; every fail-closed gate here reads a complete
    # JSON body instead.
    seen = capture(monkeypatch)
    build().classify_document("classify", "v0.1")
    assert "stream" not in seen["payload"]


# ------------------------------------------------ diagnostics stay bounded --

def test_a_huge_error_body_is_truncated_before_it_reaches_a_log(monkeypatch):
    import email.message
    import io

    leaked_page = "환자 홍길동 " * 5000

    def fake_urlopen(request, timeout):
        raise providers.urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/chat/completions", 400, "bad", email.message.Message(),
            io.BytesIO(json.dumps({"error": {"message": leaked_page}},
                                  ensure_ascii=False).encode("utf-8")))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        build().classify_document("classify", "v0.1")

    message = str(excinfo.value)
    assert "more chars omitted" in message
    assert len(message) < len(leaked_page)
    assert len(message) <= providers._OPENROUTER_ERROR_BODY_MAX_CHARS + 200
