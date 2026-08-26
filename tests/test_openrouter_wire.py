"""OpenRouter provider driven over a REAL socket, against a loopback server.

`tests/test_openrouter_provider.py` replaces `urlopen` and therefore proves
nothing about the parts urllib itself owns: whether the Authorization header is
actually transmitted, whether a Korean prompt survives the encode/decode round
trip, whether a retry re-sends a byte-identical body, whether a read timeout
surfaces as the exception the retry loop catches. Those are the failure modes an
API key would expose on the first real run, so they are exercised here instead.

The server binds 127.0.0.1 on an ephemeral port and speaks the OpenRouter wire
protocol from a scripted queue. Nothing leaves the machine and no credential is
involved -- `OPENROUTER_API_KEY` below is a literal.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import llm_providers as providers


@pytest.fixture
def no_backoff_wait(monkeypatch):
    """Record each backoff the retry loop computes, without serving the wait.

    Deliberately NOT `monkeypatch.setattr(providers.time, "sleep", ...)`:
    `providers.time` IS the stdlib module, so stubbing its `sleep` disables
    sleeping PROCESS-wide -- including the loopback server's own delay, which
    is how the read-timeout test below silently passed against a server that
    never actually delayed. Patching the provider's own backoff function keeps
    the effect inside the code under test.
    """
    delays = []
    real = providers._retry_delay

    def spy(attempt, detail=""):
        delays.append(real(attempt, detail))
        return 0.0

    monkeypatch.setattr(providers, "_retry_delay", spy)
    return delays


def header(received, name):
    """Field names are case-insensitive on the wire, and urllib capitalizes
    them (`HTTP-Referer` leaves as `Http-referer`), so a server reads them
    case-insensitively and so must this."""
    lowered = {k.lower(): v for k, v in received["headers"].items()}
    return lowered.get(name.lower())


class _Scripted(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.server.received.append({
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": raw,
        })
        if self.server.script:
            status, body, extra, delay = self.server.script.pop(0)
        else:
            status, body, extra, delay = 500, "{}", {}, 0.0
        if delay:
            # threading.Event().wait, not time.sleep: a test that stubs the
            # provider's backoff must not be able to cancel the SERVER's delay.
            threading.Event().wait(delay)
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if extra.pop("_truncate_body", None):
            # Promise more bytes than are sent, then hang up: the client's read
            # dies mid-body, which is neither an HTTPError nor a URLError.
            self.send_header("Content-Length", str(len(payload) + 500))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return
        self.send_header("Content-Length", str(len(payload)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Scripted)
    httpd.received = []
    httpd.script = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.base_url = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def provider_for(server, **env_extra):
    return providers.build_provider(
        providers.ProviderConfig(provider_name="openrouter"),
        env={
            "OPENROUTER_API_KEY": "wire-test-key-not-a-real-credential",
            "OPENROUTER_MODEL": "vendor/model-test",
            "OPENROUTER_BASE_URL": server.base_url,
            **env_extra,
        },
    )


def completion(content, *, finish_reason="stop"):
    return json.dumps({
        "id": "gen-wire",
        "model": "vendor/model-served",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }, ensure_ascii=False)


def test_the_request_urllib_actually_sends(server):
    server.script.append((200, completion("AGREE: same"), {}, 0.0))

    result = provider_for(server).compare_text("compare prompt", "ocr_compare_v0.1")

    sent = server.received[0]
    assert sent["path"] == "/api/v1/chat/completions"
    # Header names are case-insensitive on the wire and urllib capitalizes
    # them, so read them the way an HTTP server does rather than by exact key.
    assert header(sent, "Authorization") == "Bearer wire-test-key-not-a-real-credential"
    assert header(sent, "Content-Type") == "application/json"
    payload = json.loads(sent["body"].decode("utf-8"))
    assert payload["model"] == "vendor/model-test"
    assert payload["messages"] == [{"role": "user", "content": "compare prompt"}]
    assert payload["max_tokens"] == 16000
    assert "stream" not in payload, "a streamed response would arrive as SSE, not JSON"
    assert result.text == "AGREE: same"


def test_korean_survives_the_round_trip(server):
    # The entire corpus is Korean. `ensure_ascii=False` puts real UTF-8 bytes on
    # the wire, so an encoding fault here would corrupt every transcription --
    # and a mojibake page still looks like a successful call.
    page = "환자 홍길동, 진단명: 우측 요골 원위부 골절 (S52.51)"
    server.script.append((200, completion(page), {}, 0.0))

    result = provider_for(server).redact_text(f"다음 페이지를 검토: {page}", "v0.1")

    sent_body = server.received[0]["body"]
    assert "환자 홍길동".encode("utf-8") in sent_body, "prompt was not sent as UTF-8"
    assert json.loads(sent_body.decode("utf-8"))["messages"][0]["content"].endswith(page)
    assert result.text == page


def test_image_bytes_arrive_intact_as_a_data_url(server, tmp_path):
    original = bytes(range(256)) * 4
    image = tmp_path / "page.png"
    image.write_bytes(original)
    server.script.append((200, completion("page text"), {}, 0.0))

    provider_for(server).transcribe_image(image, "transcribe", "ocr_extraction_v0.1")

    content = json.loads(server.received[0]["body"].decode("utf-8"))["messages"][0]["content"]
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == original


def test_a_retry_resends_a_byte_identical_body(server, no_backoff_wait):
    # The body is built once and reused across attempts. If a retry ever sent a
    # DIFFERENT payload, the second attempt would not be the same call.
    server.script.append((429, '{"error": {"code": 429, "message": "rate limited"}}',
                          {"Retry-After": "1"}, 0.0))
    server.script.append((200, completion("page text"), {}, 0.0))

    result = provider_for(server).classify_document("classify", "v0.1")

    assert result.text == "page text"
    assert len(server.received) == 2
    assert server.received[0]["body"] == server.received[1]["body"]


def test_retry_after_from_a_real_response_header_reaches_the_backoff(
        server, no_backoff_wait):
    server.script.append((429, '{"error": {"message": "slow down"}}',
                          {"Retry-After": "4"}, 0.0))
    server.script.append((200, completion("page text"), {}, 0.0))

    provider_for(server).classify_document("classify", "v0.1")

    # The header is a FLOOR on the wait; jitter is added on top of it.
    assert no_backoff_wait and no_backoff_wait[0] >= 4


def test_a_read_timeout_raises_a_provider_error_not_a_socket_error(
        server, monkeypatch, no_backoff_wait):
    # The whole retry loop depends on a timeout arriving as an exception it
    # catches. Raised through urllib for real, a slow read surfaces as
    # TimeoutError -- not as urllib.error.URLError -- and a wrong except clause
    # here would let a bare socket error escape into the caller.
    monkeypatch.setenv(providers._COMPARE_TEXT_TIMEOUT_ENV, "1")
    for _ in range(providers._OPENROUTER_MAX_ATTEMPTS):
        server.script.append((200, completion("too late"), {}, 3.0))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider_for(server).compare_text("compare", "v0.1")

    assert "timed out after 1s" in str(excinfo.value)
    assert len(server.received) == providers._OPENROUTER_MAX_ATTEMPTS


def test_a_connection_refused_endpoint_raises_a_provider_error(no_backoff_wait):
    provider = providers.build_provider(
        providers.ProviderConfig(provider_name="openrouter"),
        # Port 1 on loopback: nothing listens, so the connect fails immediately.
        env={"OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "vendor/m",
             "OPENROUTER_BASE_URL": "http://127.0.0.1:1/api/v1"},
    )

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider.classify_document("classify", "v0.1")

    assert "openrouter call failed" in str(excinfo.value)


def test_a_200_carrying_an_error_object_is_refused_over_the_wire(server):
    server.script.append(
        (200, '{"error": {"code": 502, "message": "upstream provider is down"}}', {}, 0.0))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider_for(server).redact_text("redact", "v0.1")

    assert "upstream provider is down" in str(excinfo.value)
    assert len(server.received) == 1, "a committed 200 is not a transport failure; do not retry it"


def test_an_html_error_page_is_reported_as_non_json(server):
    # A proxy or gateway in front of the API answers HTML, not JSON.
    server.script.append((200, "<html><body>504 Gateway Time-out</body></html>", {}, 0.0))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider_for(server).classify_document("classify", "v0.1")

    assert "non-JSON response" in str(excinfo.value)


def test_a_client_error_is_reported_once_and_never_resent(server):
    server.script.append(
        (400, '{"error": {"code": 400, "message": "vendor/model-test is not a valid model"}}',
         {}, 0.0))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider_for(server).classify_document("classify", "v0.1")

    assert "not a valid model" in str(excinfo.value)
    assert len(server.received) == 1


def test_structured_output_round_trips_over_the_wire(server):
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    server.script.append((200, json.dumps({
        "id": "gen-wire-2",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "emit_result",
                                 "arguments": '{"verdict": "일치"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }, ensure_ascii=False), {}, 0.0))

    result = provider_for(server).analyze_text_structured("prompt", "v0.1", schema)

    payload = json.loads(server.received[0]["body"].decode("utf-8"))
    assert payload["tool_choice"] == {"type": "function",
                                      "function": {"name": "emit_result"}}
    assert payload["tools"][0]["function"]["parameters"]["required"] == ["verdict"]
    assert result.structured_output == {"verdict": "일치"}


def test_attribution_headers_reach_the_server_only_when_set(server):
    server.script.append((200, completion("ok"), {}, 0.0))
    provider_for(server).classify_document("classify", "v0.1")
    assert header(server.received[0], "HTTP-Referer") is None
    assert header(server.received[0], "X-Title") is None

    server.script.append((200, completion("ok"), {}, 0.0))
    provider_for(server, HARNESS_OPENROUTER_REFERER="https://example.test",
                 HARNESS_OPENROUTER_TITLE="harness").classify_document("classify", "v0.1")
    assert header(server.received[1], "HTTP-Referer") == "https://example.test"
    assert header(server.received[1], "X-Title") == "harness"


def test_a_body_that_dies_mid_read_is_a_retryable_provider_error(
        server, no_backoff_wait):
    # urlopen has already RETURNED by the time the body is read, so a
    # connection that dies here raises neither HTTPError nor URLError. Without
    # its own clause the raw http.client exception escapes the retry loop and
    # reaches the caller as something no caller handles.
    for _ in range(providers._OPENROUTER_MAX_ATTEMPTS):
        server.script.append((200, completion("half a pa"), {"_truncate_body": True}, 0.0))

    with pytest.raises(providers.ProviderExecutionError) as excinfo:
        provider_for(server).classify_document("classify", "v0.1")

    assert "did not arrive intact" in str(excinfo.value)
    assert len(server.received) == providers._OPENROUTER_MAX_ATTEMPTS


def test_the_client_names_itself(server):
    # urllib's default `Python-urllib/3.x` is exactly the User-Agent edge
    # protection in front of a public API is entitled to challenge, and that
    # rejection would read as an authentication failure.
    server.script.append((200, completion("ok"), {}, 0.0))
    provider_for(server).classify_document("classify", "v0.1")
    agent = header(server.received[0], "User-Agent")
    assert agent and not agent.startswith("Python-urllib")

    server.script.append((200, completion("ok"), {}, 0.0))
    provider_for(server, HARNESS_OPENROUTER_USER_AGENT="custom/9.9").classify_document(
        "classify", "v0.1")
    assert header(server.received[1], "User-Agent") == "custom/9.9"
