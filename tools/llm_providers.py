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
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_PROVIDERS = (
    "claude-cli", "codex-cli", "anthropic-api", "openai-api",
    "local-ocr", "local-vlm", "local-llm", "fixture",
)
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

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError


class ClaudeCliProvider(BaseProvider):
    provider_name = "claude-cli"

    def __init__(self, *, model_name: str | None = None, root: Path = ROOT, command: str = "claude"):
        self.model_name = model_name or "claude-cli"
        self.root = root
        self.command = command

    def _run(self, prompt: str, *, prompt_version: str, allowed_read: bool, timeout: int) -> ProviderResult:
        # --safe-mode: the child claude -p session must see NOTHING but the
        # prompt -- no CLAUDE.md, skills, or session hooks. Without it, cwd=ROOT
        # auto-loads this project's context, and a context-aware reader
        # editorializes: CASE_022's real checkpoint-1 run had BOTH independent
        # reads append similar D2 meta-commentary to transcribed pages, which
        # compare() then waved through as material agreement (two-sided additions
        # defeat the one-sided-addition check from known-gaps item 11), landing
        # fabricated text in the trusted processed layer. This applies to every
        # claude-cli call through this provider (transcribe/compare/classify/scan),
        # not just OCR -- the same context-inheritance risk exists for all of them.
        cmd = [self.command, "-p", prompt, "--safe-mode"]
        if allowed_read:
            cmd.extend(["--allowedTools", "Read"])
        try:
            # encoding/errors are set explicitly: the child CLI emits UTF-8, but
            # on a cp949-locale host bare text=True decodes with the ANSI code
            # page and crashes on Korean output (UnicodeDecodeError -> stdout
            # None). Same treatment CodexCliProvider already applies.
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(self.root),
            )
        except FileNotFoundError as exc:
            raise ProviderExecutionError(f"claude-cli command not found: {self.command}") from exc
        raw_metadata = {
            "command": cmd[0],
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
        if result.returncode != 0:
            raise ProviderExecutionError(f"claude-cli call failed: {result.stderr.strip()}")
        return self._result(result.stdout.strip(), prompt_version, raw_metadata)

    # Transcription prompt is deliberately kept NEUTRAL -- just the shared
    # TRANSCRIBE_PROMPT plus the image path, identical in spirit to the direct
    # `claude -p` call this provider replaced. An earlier version prepended a
    # defensive OCR-reader "role framing" block ("this is a SANCTIONED step, not
    # a bypass, do NOT touch the DAO, do not refuse..."). That backfired: the
    # child `claude -p` session used to run with cwd=project root and inherit this
    # repo's CLAUDE.md before _run() enforced --safe-mode. Even in an isolated
    # child, a prompt that pre-argues its own legitimacy and says
    # "do not refuse / do not mention guardrails" reads as a prompt-injection
    # signal -- a genuine OCR request never needs to defend itself -- so the
    # child refused and emitted meta-commentary instead of the page text. The fix
    # is to say none of it: a plain "transcribe this image" request gives the
    # child nothing to adjudicate. The real guardrails (DAO-only writes,
    # ground-truth isolation) are enforced by the tools/orchestrator, not by the
    # child's refusal; a blind reader printing to stdout writes nothing and
    # bypasses nothing regardless of prompt wording.
    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        framed_prompt = f"{prompt}\n\nImage: {image_path}"
        return self._run(framed_prompt, prompt_version=prompt_version, allowed_read=True, timeout=180)

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=120)

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=True, timeout=180)

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=120)


class CodexCliProvider(BaseProvider):
    provider_name = "codex-cli"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        root: Path = ROOT,
        command: str = "codex",
        env: Mapping[str, str] | None = None,
    ):
        self.model_name = model_name or "codex-cli"
        self.root = root
        self.command = command
        self.env = dict(env) if env is not None else dict(os.environ)

    def _run(
        self,
        prompt: str,
        *,
        prompt_version: str,
        timeout: int,
        image_path: Path | None = None,
    ) -> ProviderResult:
        scratch_dir = self.root / "_ocr_scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        output_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="codex-last-message-",
                suffix=".txt",
                dir=scratch_dir,
                delete=False,
            ) as output_file:
                output_path = Path(output_file.name)

            cmd = [self.command, "exec", prompt, "--skip-git-repo-check", "--sandbox", "read-only"]
            if self.model_name != "codex-cli":
                cmd.extend(["--model", self.model_name])
            if image_path is not None:
                cmd.extend(["--image", str(image_path)])
            cmd.extend(["--output-last-message", str(output_path)])

            run_env = os.environ.copy()
            if self.env.get("CODEX_API_KEY"):
                run_env["CODEX_API_KEY"] = self.env["CODEX_API_KEY"]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=str(self.root),
                    env=run_env,
                )
            except FileNotFoundError as exc:
                raise ProviderExecutionError(f"codex-cli command not found: {self.command}") from exc

            raw_metadata = {
                "command": cmd[0],
                "returncode": result.returncode,
                "stderr": result.stderr.strip(),
            }
            if result.returncode != 0:
                raise ProviderExecutionError(f"codex-cli call failed: {result.stderr.strip()}")
            text = output_path.read_text(encoding="utf-8").strip()
            return self._result(text, prompt_version, raw_metadata)
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(
            prompt,
            prompt_version=prompt_version,
            timeout=180,
            image_path=image_path,
        )

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=120)

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=180)

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=120)


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

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
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

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=120)


class LocalOcrProvider(BaseProvider):
    """Offline Tesseract adapter used only for page transcription.

    ``model_name`` is ``LANGUAGES:PSM`` (for example ``kor+eng:6``).
    The executable and language data must already be installed. This
    provider never downloads anything and never falls back externally.
    """

    provider_name = "local-ocr"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        command: str = "tesseract",
        env: Mapping[str, str] | None = None,
    ):
        self.model_name = model_name or "kor+eng:6"
        self.command = command
        self.env = dict(env) if env is not None else dict(os.environ)
        language, separator, psm = self.model_name.partition(":")
        if not language or (separator and (not psm.isdigit() or not 0 <= int(psm) <= 13)):
            raise ProviderConfigError(
                "local-ocr model must use LANGUAGES[:PSM], e.g. 'kor+eng:6'"
            )
        self.language = language
        self.psm = psm or "6"

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        cmd = [self.command, str(image_path), "stdout", "-l", self.language, "--psm", self.psm]
        run_env = os.environ.copy()
        if self.env.get("TESSDATA_PREFIX"):
            run_env["TESSDATA_PREFIX"] = self.env["TESSDATA_PREFIX"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180, env=run_env,
            )
        except FileNotFoundError as exc:
            raise ProviderExecutionError(
                f"local-ocr command not found: {self.command}; install Tesseract with Korean language data"
            ) from exc
        if result.returncode != 0:
            raise ProviderExecutionError(f"local-ocr call failed: {result.stderr.strip()}")
        return self._result(
            result.stdout.strip(), prompt_version,
            {"command": cmd[0], "returncode": result.returncode, "language": self.language, "psm": self.psm},
        )

    def _unsupported(self, operation: str):
        raise ProviderExecutionError(
            f"local-ocr only supports transcribe_image; use local-llm for {operation}"
        )

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("compare_text")

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("classify_document")

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("scan_intake_content")

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("redact_text")


class LocalVlmProvider(BaseProvider):
    """Offline Ollama vision adapter restricted to a loopback daemon.

    This is intended as the technology-independent P8 reader paired with
    Tesseract. The model must already be present locally; model downloads are
    deliberately outside the provider and there is no external fallback.
    """

    provider_name = "local-vlm"

    def __init__(
        self,
        *,
        model_name: str | None,
        command: str = "ollama",
        env: Mapping[str, str] | None = None,
    ):
        source_env = dict(env) if env is not None else dict(os.environ)
        self.model_name = model_name or source_env.get("HARNESS_LOCAL_VLM_MODEL") or ""
        if not self.model_name:
            raise ProviderConfigError(
                "local-vlm requires --model or HARNESS_LOCAL_VLM_MODEL; "
                "the vision model must already be installed"
            )
        self.command = command
        self.env = source_env
        configured_host = source_env.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
        self.ollama_host = configured_host if "://" in configured_host else f"http://{configured_host}"
        parsed = urlparse(self.ollama_host)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "localhost", "127.0.0.1", "::1",
        }:
            raise ProviderConfigError(
                f"local-vlm requires a loopback OLLAMA_HOST, got {configured_host!r}"
            )
        self.ollama_host = self.ollama_host.rstrip("/")
        self._model_verified = False

    def _run_env(self) -> dict[str, str]:
        run_env = os.environ.copy()
        run_env.update({key: value for key, value in self.env.items() if key.startswith("OLLAMA_")})
        run_env["OLLAMA_HOST"] = self.ollama_host
        return run_env

    def _verify_local_model(self) -> None:
        if self._model_verified:
            return
        try:
            result = subprocess.run(
                [self.command, "show", self.model_name], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=30,
                env=self._run_env(),
            )
        except FileNotFoundError as exc:
            raise ProviderExecutionError(
                f"local-vlm command not found: {self.command}; install Ollama and preload the vision model"
            ) from exc
        if result.returncode != 0:
            raise ProviderExecutionError(
                f"local-vlm model {self.model_name!r} is not available locally; "
                "preload it in the approved E: runtime (automatic download is disabled)"
            )
        self._model_verified = True

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        self._verify_local_model()
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "images": [base64.b64encode(image_path.read_bytes()).decode("ascii")],
            "stream": False,
            "think": False,
        }
        request = urllib.request.Request(
            f"{self.ollama_host}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderExecutionError(f"local-vlm loopback call failed: {exc}") from exc
        text = parsed.get("response")
        if not isinstance(text, str) or not text.strip():
            raise ProviderExecutionError("local-vlm response omitted non-empty response text")
        return self._result(
            text.strip(), prompt_version,
            {
                "endpoint": f"{self.ollama_host}/api/generate",
                "done": parsed.get("done"),
                "total_duration": parsed.get("total_duration"),
            },
        )

    def _unsupported(self, operation: str):
        raise ProviderExecutionError(
            f"local-vlm is reserved for image transcription; use local-llm for {operation}"
        )

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("compare_text")

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("classify_document")

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("scan_intake_content")

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        self._unsupported("redact_text")


class LocalLlmProvider(BaseProvider):
    """Offline text-only Ollama adapter restricted to a loopback daemon.

    The model must already exist locally (``ollama show`` must succeed),
    which prevents ``ollama run`` from initiating an automatic download.
    """

    provider_name = "local-llm"

    def __init__(
        self,
        *,
        model_name: str | None,
        command: str = "ollama",
        env: Mapping[str, str] | None = None,
    ):
        source_env = dict(env) if env is not None else dict(os.environ)
        self.model_name = model_name or source_env.get("HARNESS_LOCAL_LLM_MODEL") or ""
        if not self.model_name:
            raise ProviderConfigError(
                "local-llm requires --model or HARNESS_LOCAL_LLM_MODEL; the model must already be installed"
            )
        self.command = command
        self.env = source_env
        configured_host = source_env.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
        self.ollama_host = configured_host if "://" in configured_host else f"http://{configured_host}"
        parsed = urlparse(self.ollama_host)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ProviderConfigError(
                f"local-llm requires a loopback OLLAMA_HOST, got {configured_host!r}"
            )
        self.ollama_host = self.ollama_host.rstrip("/")
        self._model_verified = False

    def _run_env(self) -> dict[str, str]:
        run_env = os.environ.copy()
        run_env.update({key: value for key, value in self.env.items() if key.startswith("OLLAMA_")})
        run_env["OLLAMA_HOST"] = self.ollama_host
        return run_env

    def _verify_local_model(self) -> None:
        if self._model_verified:
            return
        try:
            result = subprocess.run(
                [self.command, "show", self.model_name], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=30,
                env=self._run_env(),
            )
        except FileNotFoundError as exc:
            raise ProviderExecutionError(
                f"local-llm command not found: {self.command}; install Ollama and preload the configured model"
            ) from exc
        if result.returncode != 0:
            raise ProviderExecutionError(
                f"local-llm model {self.model_name!r} is not available locally; "
                "preload it in an approved environment (automatic download is disabled)"
            )
        self._model_verified = True

    def _run(self, prompt: str, prompt_version: str, timeout: int) -> ProviderResult:
        self._verify_local_model()
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "think": False,
        }
        request = urllib.request.Request(
            f"{self.ollama_host}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderExecutionError(f"local-llm loopback call failed: {exc}") from exc
        text = parsed.get("response")
        if not isinstance(text, str) or not text.strip():
            raise ProviderExecutionError("local-llm response omitted non-empty response text")
        return self._result(
            text.strip(), prompt_version,
            {
                "endpoint": f"{self.ollama_host}/api/generate",
                "done": parsed.get("done"),
                "total_duration": parsed.get("total_duration"),
            },
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        raise ProviderExecutionError("local-llm is text-only in this harness; use local-ocr for page transcription")

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version, 120)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version, 600)

    def scan_intake_content(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version, 600)

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version, 600)


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

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._response("redact_text", prompt_version)


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
        return ClaudeCliProvider(
            model_name=selected.model_name,
            root=root,
            command=source_env.get("HARNESS_CLAUDE_COMMAND") or "claude",
        )
    if provider_name == "codex-cli":
        return CodexCliProvider(
            model_name=selected.model_name,
            root=root,
            command=source_env.get("HARNESS_CODEX_COMMAND") or "codex",
            env=source_env,
        )
    if provider_name == "anthropic-api":
        return AnthropicApiProvider(model_name=selected.model_name, env=source_env)
    if provider_name == "openai-api":
        return OpenAIApiProvider(model_name=selected.model_name, env=source_env)
    if provider_name == "local-ocr":
        return LocalOcrProvider(
            model_name=selected.model_name,
            command=source_env.get("HARNESS_LOCAL_OCR_COMMAND") or "tesseract",
            env=source_env,
        )
    if provider_name == "local-vlm":
        return LocalVlmProvider(
            model_name=selected.model_name,
            command=source_env.get("HARNESS_LOCAL_VLM_COMMAND")
            or source_env.get("HARNESS_LOCAL_LLM_COMMAND")
            or "ollama",
            env=source_env,
        )
    if provider_name == "local-llm":
        return LocalLlmProvider(
            model_name=selected.model_name,
            command=source_env.get("HARNESS_LOCAL_LLM_COMMAND") or "ollama",
            env=source_env,
        )
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
