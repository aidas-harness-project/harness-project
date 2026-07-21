"""Provider abstraction for LLM-backed pipeline calls.

This module is deliberately thin: callers own prompt construction and
domain-specific parsing, while providers own how a prompt reaches an
execution backend. That lets OCR, classification, and intake checks share
one provider-selection path without coupling their safety rules to any one
CLI or API surface.

Providers here are all LLM-backed (CLI or HTTP API). An earlier revision
also shipped an offline Tesseract/Ollama trio (``local-ocr``/``local-vlm``/
``local-llm``); it was removed because the pinned local models never
transcribed real Korean claim pages and the runtime was single-machine
(Windows/E:-drive) only -- see the PR #8 review and open-decisions.md #3/#4.
A genuinely technology-independent P8 reader (a real OCR engine) is deferred,
not replaced by these.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_PROVIDERS = (
    "claude-cli", "codex-cli", "anthropic-api", "openai-api", "fixture",
)
DEFAULT_PROVIDER = "claude-cli"
DEFAULT_ENV_PREFIX = "HARNESS_LLM"

# claude-cli transient-failure retry: total attempts and fixed sleep between
# them. Applies only to subprocess-level failures (non-zero exit / timeout),
# never to content agreement -- see ClaudeCliProvider._run.
_CLAUDE_CLI_MAX_ATTEMPTS = 3
_CLAUDE_CLI_RETRY_SLEEP_SECONDS = 2.0
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# A child subprocess that reads untrusted claim-document images has no reason
# to hold other providers' credentials. Any env var whose name ends in one of
# these suffixes is stripped from a CLI child's environment unless it belongs
# to that CLI's own provider family (see _child_safe_env). A denylist (not an
# allowlist) so ordinary vars like PATH/HOME are never accidentally dropped.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_ACCESS_KEY")


class ProviderConfigError(RuntimeError):
    """Raised when a provider is selected but not usable as configured."""


class ProviderExecutionError(RuntimeError):
    """Raised when a configured provider fails during execution."""


def _child_safe_env(*, keep_prefixes: Sequence[str]) -> dict[str, str]:
    """A copy of os.environ with foreign provider secrets removed.

    Keeps any secret-shaped var whose name starts with one of ``keep_prefixes``
    (the CLI's own provider family, which it needs to authenticate); drops every
    other secret-shaped var so a document-reading child can't exfiltrate, e.g.,
    OPENAI_API_KEY through a prompt-injected transcription.
    """
    safe = {}
    for key, value in os.environ.items():
        is_secret = any(key.upper().endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES)
        if is_secret and not any(key.upper().startswith(prefix) for prefix in keep_prefixes):
            continue
        safe[key] = value
    return safe


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
    # Provider-reported completion signal where one exists (HTTP APIs). None for
    # CLI providers, which expose no finish reason. A value other than the
    # provider's normal-completion marker means the text may be truncated -- the
    # provider raises rather than returning a partial page (see _post_responses).
    finish_reason: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "raw_metadata": self.raw_metadata,
            "finish_reason": self.finish_reason,
        }


class BaseProvider:
    provider_name: str
    model_name: str

    def _result(
        self,
        text: str,
        prompt_version: str,
        raw_metadata: dict[str, Any] | None = None,
        *,
        finish_reason: str | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            provider_name=self.provider_name,
            model_name=self.model_name,
            prompt_version=prompt_version,
            text=text,
            raw_metadata=raw_metadata or {},
            finish_reason=finish_reason,
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise NotImplementedError

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
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
        # Pass the configured model through. Without this the model recorded in
        # provenance metadata is a lie (the CLI silently uses its own default),
        # and two readers configured with different models would be
        # indistinguishable. "claude-cli" is the no-model-configured sentinel.
        if self.model_name and self.model_name != "claude-cli":
            cmd.extend(["--model", self.model_name])
        if allowed_read:
            cmd.extend(["--allowedTools", "Read"])

        # The child reads untrusted claim images; strip every non-Anthropic
        # secret from its environment so a prompt-injected read can't exfiltrate
        # another provider's key (see _child_safe_env).
        run_env = _child_safe_env(keep_prefixes=("ANTHROPIC", "CLAUDE"))

        # Bounded retry on transient subprocess-level failures. A single
        # failed claude-cli call otherwise kills an entire multi-page run:
        # checkpoint 1 only persists ocr_result after every page finishes, so
        # one hiccup on page N of a 75-page document (CASE_003/DOC_008 died
        # exactly this way at page 24 on "no stdin data received") discards all
        # prior pages. This retries ONLY the subprocess call itself
        # (FileNotFoundError is not transient and is not retried); it does NOT
        # touch P8 -- content agreement/disagreement is judged by compare(),
        # not here, so no disagreement tolerance is affected.
        last_exc: ProviderExecutionError | None = None
        for attempt in range(_CLAUDE_CLI_MAX_ATTEMPTS):
            try:
                # stdin=DEVNULL: without it the child inherits the parent's
                # stdin and blocks ~3s waiting for input it will never get
                # ("Warning: no stdin data received in 3s..."), which both
                # slows every call and, on a live pipe (PowerShell background),
                # intermittently returns non-zero. Closing stdin is exactly
                # what the CLI's own warning recommends ("< /dev/null").
                #
                # encoding/errors are set explicitly: the child CLI emits
                # UTF-8, but on a cp949-locale host bare text=True decodes with
                # the ANSI code page and crashes on Korean output
                # (UnicodeDecodeError -> stdout None). Same treatment
                # CodexCliProvider already applies.
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    env=run_env,
                )
            except FileNotFoundError as exc:
                raise ProviderExecutionError(f"claude-cli command not found: {self.command}") from exc
            except subprocess.TimeoutExpired as exc:
                last_exc = ProviderExecutionError(f"claude-cli call timed out after {timeout}s")
                last_exc.__cause__ = exc
            else:
                if result.returncode == 0:
                    raw_metadata = {
                        "command": cmd[0],
                        "returncode": result.returncode,
                        "stderr": result.stderr.strip(),
                        "attempts": attempt + 1,
                    }
                    return self._result(result.stdout.strip(), prompt_version, raw_metadata)
                last_exc = ProviderExecutionError(f"claude-cli call failed: {result.stderr.strip()}")

            if attempt < _CLAUDE_CLI_MAX_ATTEMPTS - 1:
                time.sleep(_CLAUDE_CLI_RETRY_SLEEP_SECONDS)

        assert last_exc is not None
        raise last_exc

    # Transcription prompt is deliberately kept NEUTRAL -- no defensive
    # "this is a SANCTIONED step, do not refuse" framing. An earlier version
    # prepended exactly that role-framing block and it backfired: a prompt that
    # pre-argues its own legitimacy reads as a prompt-injection signal -- a
    # genuine OCR request never needs to defend itself -- so the child refused
    # and emitted meta-commentary instead of the page text. That lesson still
    # holds and is not being reverted here.
    #
    # The image is referenced as an explicit imperative ("Read the image file at
    # {path} and then ...") rather than a trailing "Image: {path}" label.
    # Investigation (2026-07-16) found the label form fails deterministically
    # (9/9, with and without --safe-mode): the model reads "Image: {path}" as
    # descriptive metadata about an attachment that -- via a text-only `claude -p`
    # call -- never actually arrives, so it never invokes the Read tool. Making
    # the read an explicit instruction resolves it. This carries no
    # self-legitimizing or anti-refusal language, so it gives the child nothing
    # to treat as a prompt-injection signal.
    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        framed_prompt = f"Read the image file at {image_path} and then: {prompt}"
        return self._run(framed_prompt, prompt_version=prompt_version, allowed_read=True, timeout=180)

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, allowed_read=False, timeout=120)

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        # The child opens the pages itself via its Read tool. Use the same
        # explicit-imperative form transcribe_image uses -- a trailing
        # "Image: {path}" label is read as metadata about a never-arriving
        # attachment and the Read tool is never invoked (2026-07-16 finding). An
        # HTTP provider attaches the images instead (see OpenAIApiProvider).
        if image_paths:
            refs = "\n".join(f"Read the image file at {p}." for p in image_paths)
            prompt = f"{prompt}\n\n{refs}"
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
        image_paths: Sequence[Path] | None = None,
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
            for image_path in image_paths or ():
                cmd.extend(["--image", str(image_path)])
            cmd.extend(["--output-last-message", str(output_path)])

            # Start from a secret-scrubbed environment, then re-add only Codex's
            # own key -- a document-reading child shouldn't carry other
            # providers' credentials.
            run_env = _child_safe_env(keep_prefixes=("CODEX",))
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
            image_paths=[image_path],
        )

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._run(prompt, prompt_version=prompt_version, timeout=120)

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        return self._run(
            prompt, prompt_version=prompt_version, timeout=180, image_paths=image_paths
        )

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
        # No silent fabrication: a made-up "<provider>-model" string only fails
        # later with an opaque 404 at the first page. Fail at selection instead.
        env_hint = " or ".join(self.model_env_names) if self.model_env_names else "the provider env"
        raise ProviderConfigError(
            f"{self.provider_name} requires a model name via --model or {env_hint}"
        )

    def _not_implemented(self) -> ProviderExecutionError:
        return ProviderExecutionError(
            f"{self.provider_name} is selectable, but API execution is not implemented yet"
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        raise self._not_implemented()

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
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

        # Truncation guard: a response cut off at the token cap comes back with
        # status "incomplete" and PARTIAL output_text. Returning that as "the
        # transcription"/"the redaction" would silently land a half a page in
        # the trusted layer. Refuse it -- the caller fails closed rather than
        # trusting a truncated read.
        status = parsed.get("status")
        if status == "incomplete":
            reason = (parsed.get("incomplete_details") or {}).get("reason", "unknown")
            raise ProviderExecutionError(
                f"openai-api response was truncated (status=incomplete, reason={reason}); "
                "refusing to use a partial result"
            )

        text = _extract_openai_output_text(parsed)
        if text is None:
            raise ProviderExecutionError("openai-api response did not contain output_text")
        return self._result(
            text.strip(),
            prompt_version,
            {
                "response_id": parsed.get("id"),
                "status": status,
                "http_status": status_code,
                "usage": parsed.get("usage"),
            },
            finish_reason=status,
        )

    def transcribe_image(self, image_path: Path, prompt: str, prompt_version: str) -> ProviderResult:
        input_payload = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                *_image_content_parts([image_path]),
            ],
        }]
        return self._post_responses(input_payload, prompt_version=prompt_version, timeout=180)

    def compare_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=60)

    def classify_document(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=120)

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
        # The whole point of the D2 scan is that the model SEES the page images.
        # Attach them as image content parts; a bare text prompt naming file
        # paths reaches the API as dead strings the server cannot open.
        input_payload = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                *_image_content_parts(image_paths or ()),
            ],
        }]
        return self._post_responses(input_payload, prompt_version=prompt_version, timeout=180)

    def redact_text(self, prompt: str, prompt_version: str) -> ProviderResult:
        return self._post_responses(prompt, prompt_version=prompt_version, timeout=120)


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

    def scan_intake_content(
        self, prompt: str, prompt_version: str, image_paths: Sequence[Path] | None = None
    ) -> ProviderResult:
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
    encoded = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _image_content_parts(image_paths: Sequence[Path]) -> list[dict[str, Any]]:
    return [
        {"type": "input_image", "image_url": _image_data_url(Path(p)), "detail": "high"}
        for p in image_paths
    ]


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
