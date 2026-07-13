"""Provider abstraction for LLM-backed pipeline calls.

This module is deliberately thin: callers own prompt construction and
domain-specific parsing, while providers own how a prompt reaches an
execution backend. That lets OCR, classification, and intake checks share
one provider-selection path without coupling their safety rules to any one
CLI or API surface.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_PROVIDERS = ("claude-cli", "anthropic-api", "openai-api", "fixture")
DEFAULT_PROVIDER = "claude-cli"
DEFAULT_ENV_PREFIX = "HARNESS_LLM"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class ProviderConfigError(RuntimeError):
    """Raised when a provider is selected but not usable as configured."""


class ProviderExecutionError(RuntimeError):
    """Raised when a configured provider fails during execution."""


@dataclass(frozen=True)
class ProviderConfig:
    provider_name: str = DEFAULT_PROVIDER
    model_name: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    provider_name: str
    model_name: str
    prompt_version: str
    text: str
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "raw_metadata": self.raw_metadata,
        }


class BaseProvider:
    provider_name: str
    model_name: str

    def _result(self, text: str, prompt_version: str, raw_metadata: dict[str, Any] | None = None) -> ProviderResult:
        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            prompt_version=prompt_version,
            text=text,
            raw_metadata=raw_metadata or {},
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError


class ClaudeCliProvider(BaseProvider):
    provider_name = "claude-cli"

    def __init__(self, *, model_name: str | None = None, root: Path = ROOT, command: str = "claude"):
        self.model_name = model_name or "claude-cli"
        self.root = root
        self.command = command

    def _run(self, prompt: str, *, prompt_version: str, allowed_read: bool, timeout: int) -> ProviderResult:
        cmd = [self.command, "-p", prompt]
        if allowed_read:
            cmd.extend(["--allowedTools", "Read"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(self.root))
        raw_metadata = {
            "command": cmd[0],
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
        if result.returncode != 0:
            raise ProviderExecutionError(f"claude-cli call failed: {result.stderr.strip()}")
        return self._result(result.stdout.strip(), prompt_version, raw_metadata)

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(f"{prompt}\n\nImage: {image_path}", prompt_version=prompt_version, allowed_read=True, timeout=180)

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=120)

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=True, timeout=180)


class _ApiProviderStub(BaseProvider):
    required_key_env: str
    model_env_names: tuple[str, ...] = ()

    def __init__(self, *, model_name: str | None, env: Mapping[str, str]):
        api_key = env.get(self.required_key_env)
        if not api_key:
            raise ProviderConfigError(f"{self.provider_name} requires {self.required_key_env}")
        self.api_key = api_key
        self.model_name = model_name or self._default_model_name(env)

    def _default_model_name(self, env: Mapping[str, str]) -> str:
        for env_name in self.model_env_names:
            if env.get(env_name):
                return env[env_name]
        return f"{self.provider_name}-model"

    def _not_implemented(self) -> ProviderExecutionError:
        return ProviderExecutionError(
            f"{self.provider_name} is selectable, but API execution is not implemented in this issue yet"
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()


class AnthropicApiProvider(_ApiProviderStub):
    provider_name = "anthropic-api"
    required_key_env = "ANTHROPIC_API_KEY"
    model_env_names = ("HARNESS_ANTHROPIC_MODEL", "ANTHROPIC_MODEL")


class OpenAIApiProvider(_ApiProviderStub):
    provider_name = "openai-api"
    required_key_env = "OPENAI_API_KEY"
    model_env_names = ("HARNESS_OPENAI_MODEL", "OPENAI_MODEL")
    default_model_name = "gpt-4.1"

    def __init__(
        self,
        *,
        model_name: str | None,
        env: Mapping[str, str],
        base_url: str | None = None,
    ):
        super().__init__(model_name=model_name, env=env)
        self.model_name = model_name or self._default_model_name(env)
        self.base_url = (base_url or env.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).rstrip("/")

    def _default_model_name(self, env: Mapping[str, str]) -> str:
        for env_name in self.model_env_names:
            if env.get(env_name):
                return env[env_name]
        return self.default_model_name

    def _post_responses(self, input_payload, *, prompt_version: str, timeout: int) -> ProviderResult:
        payload = {"model": self.model_name, "input": input_payload}
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
                status_code = getattr(response, "status", None)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise ProviderExecutionError(f"openai-api call failed ({exc.code}): {error_body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderExecutionError(f"openai-api call failed: {exc.reason}") from exc

        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ProviderExecutionError(f"openai-api returned non-JSON response: {response_body!r}") from exc

        text = _extract_openai_output_text(parsed)
        if text is None:
            raise ProviderExecutionError("openai-api response did not contain output_text")
        return self._result(
            text.strip(),
            prompt_version,
            {
                "response_id": parsed.get("id"),
                "status": parsed.get("status"),
                "http_status": status_code,
                "usage": parsed.get("usage"),
            },
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        image_url = _image_data_url(image_path)
        input_payload = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": image_url, "detail": "high"},
            ],
        }]
        return self._post_responses(input_payload, prompt_version=prompt_version, timeout=180)

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=120)

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=180)


class FixtureProvider(BaseProvider):
    provider_name = "fixture"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        text: str = "",
        responses: Mapping[str, str] | None = None,
    ):
        self.model_name = model_name or "fixture"
        self.text = text
        self.responses = dict(responses or {})

    def _response(self, key: str, prompt_version: str) -> ProviderResult:
        return self._result(self.responses.get(key, self.text), prompt_version, {"fixture_key": key})

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("transcribe_image", prompt_version)

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("compare_text", prompt_version)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("classify_document", prompt_version)

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("scan_intake_content", prompt_version)


def add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, help="LLM provider backend")
    parser.add_argument("--model", help="Provider model name or deployment identifier")


def parse_provider_config(
    args: argparse.Namespace | None = None,
    *,
    env: Mapping[str, str] | None = None,
    env_prefix: str = DEFAULT_ENV_PREFIX,
    default_provider: str = DEFAULT_PROVIDER,
) -> ProviderConfig:
    source_env = env if env is not None else os.environ
    arg_provider = getattr(args, "provider", None) if args is not None else None
    arg_model = getattr(args, "model", None) if args is not None else None
    env_provider = source_env.get(f"{env_prefix}_PROVIDER")
    env_model = source_env.get(f"{env_prefix}_MODEL")

    provider_name = _normalize_provider_name(arg_provider or env_provider or default_provider)
    model_name = arg_model or env_model
    return ProviderConfig(provider_name=provider_name, model_name=model_name)


def build_provider(
    config: ProviderConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: Path = ROOT,
    fixture_responses: Mapping[str, str] | None = None,
):
    source_env = env if env is not None else os.environ
    selected = config or parse_provider_config(env=source_env)
    provider_name = _normalize_provider_name(selected.provider_name)

    if provider_name == "claude-cli":
        return ClaudeCliProvider(model_name=selected.model_name, root=root)
    if provider_name == "anthropic-api":
        return AnthropicApiProvider(model_name=selected.model_name, env=source_env)
    if provider_name == "openai-api":
        return OpenAIApiProvider(model_name=selected.model_name, env=source_env)
    if provider_name == "fixture":
        return FixtureProvider(model_name=selected.model_name, responses=fixture_responses)
    raise ProviderConfigError(f"unsupported provider {selected.provider_name!r}")


def _normalize_provider_name(provider_name: str) -> str:
    normalized = provider_name.strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ProviderConfigError(
            f"unsupported provider {provider_name!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return normalized


def _image_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_openai_output_text(response: Mapping[str, Any]) -> str | None:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    parts: list[str] = []
    for item in response.get("output", []) or []:
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, Mapping):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    if parts:
        return "".join(parts)
    return None
